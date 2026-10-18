"""HU024 sin infraestructura: transición, requisitos, idempotencia, propiedad y versión."""

import logging
import unittest
from datetime import timedelta
from uuid import UUID, uuid4

from fakes import NOW, FixedClock, InMemoryStore, location
from publication_fakes import (
    InMemoryPublicationUnitOfWork,
    PublishPermissions,
    draft,
    with_details,
)

from events_management.application.commands.publish_event import (
    PublicationResult,
    PublishEvent,
    PublishEventCommand,
)
from events_management.application.errors import IdempotencyKeyReused, PermissionRequired
from events_management.application.ports import Caller, PermissionPolicy
from events_management.domain.sport_events.model import (
    EventNotFound,
    EventStatus,
    Location,
    SportEvent,
)
from events_management.domain.validation import InvalidSportEvent


class OnlyCreate:
    """Organizador sin `events.publish`."""

    def allows(self, actor_type: str, permission: str) -> bool:
        return actor_type == "organizer" and permission == "events.create"


class PublicationDomainTest(unittest.TestCase):
    def test_publish_moves_draft_once_and_stamps_the_server_instant(self) -> None:
        event = draft(uuid4())
        at = NOW + timedelta(hours=3)

        published = event.publish(at)

        self.assertEqual(published.status, EventStatus.PUBLISHED)
        self.assertEqual(published.version, event.version + 1)
        self.assertEqual((published.published_at, published.updated_at), (at, at))
        self.assertEqual(published.created_at, event.created_at)
        self.assertEqual(published.details, event.details)
        with self.assertRaises(ValueError):
            published.publish(at)


class PublishEventTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = InMemoryStore()
        self.clock = FixedClock()
        self.organizer = Caller(uuid4(), "organizer")
        self.publish = self.use_case()

    def use_case(
        self,
        permissions: PermissionPolicy | None = None,
        before_commit: list[object] | None = None,
    ) -> PublishEvent:
        hooks = before_commit if before_commit is not None else []

        def unit_of_work() -> InMemoryPublicationUnitOfWork:
            hook = hooks.pop(0) if hooks else None
            return InMemoryPublicationUnitOfWork(self.store, before_commit=hook)  # type: ignore[arg-type]

        return PublishEvent(
            unit_of_work=unit_of_work,
            permissions=permissions or PublishPermissions(),
            clock=self.clock,
        )

    def stored(self, event: SportEvent | None = None) -> SportEvent:
        event = event or draft(self.organizer.user_id)
        self.store.events[event.event_id] = event
        return event

    def publish_event(
        self, event_id: UUID, key: UUID | None = None, caller: Caller | None = None
    ) -> PublicationResult:
        return self.publish(
            caller or self.organizer,
            PublishEventCommand(
                event_id=event_id, idempotency_key=key or uuid4(), correlation_id=uuid4()
            ),
        )

    def test_publishes_with_one_version_bump_and_one_outbox_event(self) -> None:
        """CA1 y CA5: `PUBLISHED`, versión + 1, `published_at` y un único evento."""
        event = self.stored()
        key = uuid4()
        self.clock.advance(timedelta(hours=2))

        with self.assertLogs("sportapp.audit", level=logging.INFO) as logs:
            result = self.publish_event(event.event_id, key)

        published = self.store.events[event.event_id]
        self.assertFalse(result.replayed or result.already_published)
        self.assertEqual((published.status, published.version), (EventStatus.PUBLISHED, 2))
        self.assertEqual(published.published_at, self.clock.now())
        [message] = self.store.outbox
        self.assertEqual(message.event_type, "evento.publicado.v1")
        self.assertEqual((message.aggregate_id, message.aggregate_version), (event.event_id, 2))
        self.assertEqual(message.causation_id, str(key))
        self.assertEqual(message.payload["status"], "PUBLISHED")
        self.assertEqual(message.payload["published_at"], "2026-10-12T23:00:00Z")
        audit = logs.records[0].fields  # type: ignore[attr-defined]
        self.assertEqual(
            {k: audit[k] for k in ("result", "event_id", "organizer_id")},
            {
                "result": "published",
                "event_id": str(event.event_id),
                "organizer_id": str(self.organizer.user_id),
            },
        )
        self.assertNotIn("Carrera", str(audit))
        self.assertNotIn("Simón", str(audit))

    def test_same_key_returns_the_original_response(self) -> None:
        """CA4: misma clave → la respuesta original, sin otra versión ni evento."""
        event = self.stored()
        key = uuid4()
        first = self.publish_event(event.event_id, key)
        self.clock.advance(timedelta(minutes=5))

        again = self.publish_event(event.event_id, key)

        self.assertTrue(again.replayed)
        self.assertEqual(again.event, first.event)
        self.assertEqual(len(self.store.outbox), 1)

    def test_already_published_with_another_key_is_current_state_without_event(self) -> None:
        """CA4: otra clave → `200` con el estado vigente, sin versión nueva ni evento."""
        event = self.stored()
        first = self.publish_event(event.event_id)
        other_key = uuid4()

        again = self.publish_event(event.event_id, other_key)
        replay = self.publish_event(event.event_id, other_key)

        self.assertTrue(again.already_published)
        self.assertFalse(again.replayed)
        self.assertEqual(again.event, first.event)
        self.assertEqual(self.store.events[event.event_id].version, 2)
        self.assertEqual(len(self.store.outbox), 1)
        self.assertTrue(replay.replayed)

    def test_same_key_with_another_event_is_reused(self) -> None:
        """CA4: misma clave con otro evento → `idempotency_key_reused`, nada cambia."""
        first, second = self.stored(), self.stored()
        key = uuid4()
        self.publish_event(first.event_id, key)

        with self.assertRaises(IdempotencyKeyReused):
            self.publish_event(second.event_id, key)

        self.assertEqual(self.store.events[second.event_id].status, EventStatus.DRAFT)
        self.assertEqual(len(self.store.outbox), 1)

    def test_expired_key_is_a_new_request(self) -> None:
        first, second = self.stored(), self.stored()
        key = uuid4()
        self.publish_event(first.event_id, key)
        self.clock.advance(timedelta(hours=24, seconds=1))

        result = self.publish_event(second.event_id, key)

        self.assertFalse(result.replayed)
        self.assertEqual(self.store.events[second.event_id].status, EventStatus.PUBLISHED)

    def test_foreign_or_missing_event_is_not_found_and_nothing_changes(self) -> None:
        """CA3: ajeno o inexistente → `event_not_found`, sin clave ni evento."""
        foreign = self.stored(draft(uuid4()))

        for event_id in (foreign.event_id, uuid4()):
            with self.subTest(event_id=event_id), self.assertRaises(EventNotFound):
                self.publish_event(event_id)

        self.assertEqual(self.store.events[foreign.event_id].status, EventStatus.DRAFT)
        self.assertEqual((self.store.outbox, self.store.idempotency), ([], {}))

    def test_only_organizer_with_publish_permission(self) -> None:
        """CA6: otro actor o un organizador sin `events.publish` → `permission_required`."""
        event = self.stored()
        cases = (
            (Caller(uuid4(), "athlete"), PublishPermissions()),
            (Caller(uuid4(), "service_provider"), PublishPermissions()),
            (self.organizer, OnlyCreate()),
        )
        for caller, permissions in cases:
            with self.subTest(actor=caller.actor_type), self.assertRaises(PermissionRequired):
                self.use_case(permissions)(
                    caller, PublishEventCommand(event_id=event.event_id, idempotency_key=uuid4())
                )
        self.assertEqual(self.store.events[event.event_id].status, EventStatus.DRAFT)

    def test_start_in_the_past_is_rejected_and_stays_draft(self) -> None:
        """CA2: inicio ya pasado → `not_in_future`, el evento sigue `DRAFT` sin datos parciales."""
        event = self.stored(draft(self.organizer.user_id, starts_in=timedelta(hours=1)))
        self.clock.advance(timedelta(hours=1))

        with self.assertRaises(InvalidSportEvent) as raised:
            self.publish_event(event.event_id)

        self.assertEqual(
            [(v.field, v.issue.value) for v in raised.exception.violations],
            [("starts_at", "not_in_future")],
        )
        self.assertEqual(self.store.events[event.event_id], event)
        self.assertEqual((self.store.outbox, self.store.idempotency), ([], {}))

    def test_each_adr028_rule_is_revalidated_on_the_persisted_aggregate(self) -> None:
        """CA2: el agregado persistido debe cumplir de nuevo ADR-028; se enumeran todas."""
        bad_location = Location(**{**location(), "country_code": "ZZ", "city": "B"})
        cases = {
            "name": ({"name": "ab"}, [("name", "invalid_length")]),
            "description": ({"description": "corta"}, [("description", "invalid_length")]),
            "discipline": ({"discipline": "x"}, [("discipline", "invalid_length")]),
            "location": (
                {"location": bad_location},
                [("location.city", "invalid_length"), ("location.country_code", "unknown_value")],
            ),
            "duration": ({"duration_minutes": 10}, [("duration_minutes", "out_of_range")]),
            "capacity": ({"capacity": 0}, [("capacity", "out_of_range")]),
            "time_zone": ({"time_zone": "Marte/Olimpo"}, [("time_zone", "time_zone_unknown")]),
        }
        for name, (changes, expected) in cases.items():
            with self.subTest(rule=name):
                event = self.stored(with_details(draft(self.organizer.user_id), **changes))
                with self.assertRaises(InvalidSportEvent) as raised:
                    self.publish_event(event.event_id)
                found = [(v.field, v.issue.value) for v in raised.exception.violations]
                self.assertEqual(found, expected)
                self.assertEqual(self.store.events[event.event_id].status, EventStatus.DRAFT)
        self.assertEqual(self.store.outbox, [])

    def test_concurrent_same_key_returns_the_first_result(self) -> None:
        """Otro reintento con la misma clave confirmó entre la lectura y el COMMIT."""
        event = self.stored()
        key = uuid4()
        rival = self.use_case()
        hooks: list[object] = [
            None,
            lambda: rival(self.organizer, PublishEventCommand(event.event_id, key)),
        ]
        racing = self.use_case(before_commit=hooks)

        result = racing(self.organizer, PublishEventCommand(event.event_id, key))

        self.assertTrue(result.replayed)
        self.assertEqual(len(self.store.outbox), 1)
        self.assertEqual(self.store.events[event.event_id].version, 2)


if __name__ == "__main__":
    unittest.main()
