"""HU041 sin infraestructura: reglas, comando de inscripción, API, consultas y proyección."""

import unittest
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

from fakes import FixedClock, InMemoryProjectionUnitOfWork, ReadStore
from fastapi.testclient import TestClient
from registration_fakes import (
    NOW,
    InMemoryEventDetailReader,
    InMemoryRegistrationReader,
    InMemoryRegistrationStore,
    InMemoryRegistrationUnitOfWork,
    calendar_view,
    summary,
)
from test_create_event import VERIFIER, token

from events_management.api.registrations import build_registrations_router
from events_management.application.commands.register_for_event import (
    RegisterForEvent,
    RegisterForEventCommand,
)
from events_management.application.errors import (
    EventRegistrationExists,
    IdempotencyKeyReused,
    PermissionRequired,
)
from events_management.application.messages import (
    RegistrationCreatedMessage,
    parse_sport_event_message,
)
from events_management.application.ports import Caller, EventDetailView, RegistrationView
from events_management.application.projections.event_projection import (
    EventsProjector,
    ProjectionConflict,
    ProjectionOutcome,
)
from events_management.application.queries.registrations import (
    GetPublishedEvent,
    InvalidRegistrationsQuery,
    ListMyRegistrations,
    RegistrationsFilters,
)
from events_management.domain.registrations.events import registered_payload
from events_management.domain.registrations.model import (
    EventFull,
    EventNotAvailable,
    EventRegistration,
    EventRegistrationClosed,
    RegistrationStatus,
)
from events_management.domain.sport_events.model import EventStatus
from sportapp_platform.app import create_service_app
from sportapp_platform.config import ServiceSettings


class RegisterForEventTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = InMemoryRegistrationStore()
        self.clock = FixedClock(NOW)
        self.athlete = Caller(uuid4(), "athlete")
        self.event_id = self.store.publish(capacity=2)
        self.register = self.use_case()

    def use_case(self) -> RegisterForEvent:
        return RegisterForEvent(
            unit_of_work=lambda: InMemoryRegistrationUnitOfWork(self.store), clock=self.clock
        )

    def command(
        self, event_id: UUID | None = None, key: UUID | None = None
    ) -> RegisterForEventCommand:
        return RegisterForEventCommand(
            event_id=event_id or self.event_id,
            idempotency_key=key or uuid4(),
            correlation_id=uuid4(),
        )

    def test_registers_with_its_event_in_one_commit(self) -> None:
        """CA1 y CA7."""
        command = self.command()

        result = self.register(self.athlete, command)

        registration = result.registration.registration
        self.assertEqual(
            (registration.status, registration.version), (RegistrationStatus.REGISTERED, 1)
        )
        self.assertEqual(registration.athlete_id, self.athlete.user_id)
        self.assertIsNone(registration.applied_fee)
        [message] = self.store.outbox
        self.assertEqual(message.event_type, "usuario.inscrito.v1")
        self.assertEqual(
            (message.aggregate_id, message.aggregate_version), (registration.registration_id, 1)
        )
        self.assertEqual(message.causation_id, str(command.idempotency_key))
        self.assertEqual(message.payload, registered_payload(registration))
        self.assertNotIn("name", message.payload, "sin datos del evento en el payload")

    def test_only_published_future_events_with_room(self) -> None:
        """CA2, CA3 y CA5 (cupo exacto)."""
        draft = self.store.publish(status=EventStatus.DRAFT)
        started = self.store.publish(starts_at=NOW)
        cases = [
            (uuid4(), EventNotAvailable),
            (draft, EventNotAvailable),
            (started, EventRegistrationClosed),
        ]
        for event_id, error in cases:
            with self.subTest(error=error.__name__), self.assertRaises(error):
                self.register(self.athlete, self.command(event_id))
        self.register(Caller(uuid4(), "athlete"), self.command())
        self.register(Caller(uuid4(), "athlete"), self.command())
        with self.assertRaises(EventFull):
            self.register(self.athlete, self.command())
        self.assertEqual(len(self.store.registrations), 2)

    def test_one_registration_per_athlete(self) -> None:
        """CA4: con otra clave → existente, sin duplicar."""
        first = self.register(self.athlete, self.command())

        with self.assertRaises(EventRegistrationExists) as raised:
            self.register(self.athlete, self.command())

        self.assertEqual(
            raised.exception.registration_id, first.registration.registration.registration_id
        )
        self.assertEqual(len(self.store.outbox), 1)

    def test_idempotent_retry_and_key_reuse(self) -> None:
        """CA6."""
        key = uuid4()
        first = self.register(self.athlete, self.command(key=key))
        retry = self.register(self.athlete, self.command(key=key))
        other_event = self.store.publish()

        self.assertTrue(retry.replayed)
        self.assertEqual(retry.registration, first.registration)
        self.assertEqual(len(self.store.outbox), 1)
        with self.assertRaises(IdempotencyKeyReused):
            self.register(self.athlete, self.command(other_event, key))

    def test_concurrent_retry_with_the_same_key_returns_the_original(self) -> None:
        key = uuid4()
        winner = self.use_case()
        units = iter(
            [
                InMemoryRegistrationUnitOfWork(self.store),
                InMemoryRegistrationUnitOfWork(
                    self.store, lambda: winner(self.athlete, self.command(key=key))
                ),
                InMemoryRegistrationUnitOfWork(self.store),
            ]
        )
        loser = RegisterForEvent(unit_of_work=units.__next__, clock=self.clock)

        result = loser(self.athlete, self.command(key=key))

        self.assertTrue(result.replayed)
        self.assertEqual((len(self.store.registrations), len(self.store.outbox)), (1, 1))

    def test_only_athletes(self) -> None:
        """CA9."""
        for actor in ("organizer", "service_provider"):
            with self.subTest(actor=actor), self.assertRaises(PermissionRequired):
                self.register(Caller(uuid4(), actor), self.command())

    def test_audit_without_bodies(self) -> None:
        with self.assertLogs("sportapp.audit", level="INFO") as logs:
            result = self.register(self.athlete, self.command())

        fields = logs.records[0].fields  # type: ignore[attr-defined]
        self.assertEqual(logs.records[0].getMessage(), "audit.event_registration_created")
        self.assertEqual(
            fields["registration_id"], str(result.registration.registration.registration_id)
        )
        self.assertEqual((fields["result"], fields["event_id"]), ("registered", str(self.event_id)))
        self.assertNotIn("Carrera", str(fields))


def registration(athlete_id: UUID, event_id: UUID | None = None) -> EventRegistration:
    return EventRegistration(
        registration_id=uuid4(),
        event_id=event_id or uuid4(),
        athlete_id=athlete_id,
        status=RegistrationStatus.REGISTERED,
        registered_at=NOW,
        applied_fee=None,
        version=1,
        created_at=NOW,
        updated_at=NOW,
    )


class RegistrationsApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = InMemoryRegistrationStore()
        self.athlete_id = uuid4()
        self.event_id = self.store.publish(capacity=1)
        mine = registration(self.athlete_id)
        self.views = [RegistrationView(mine, summary(NOW + timedelta(days=5)))]
        self.details = {
            self.event_id: EventDetailView(
                calendar_view(self.event_id, NOW + timedelta(days=30)),
                "Descripción del evento",
                1,
                None,
            ),
            mine.event_id: EventDetailView(
                calendar_view(mine.event_id, NOW + timedelta(days=5)), "Otro evento", 50, mine
            ),
        }
        app = create_service_app(
            service_name="events-management",
            title="t",
            api_version="t",
            settings=ServiceSettings(app_env="test"),
            routers=[
                build_registrations_router(
                    verify_token=VERIFIER.verify,
                    get_event=GetPublishedEvent(reader=InMemoryEventDetailReader(self.details)),
                    register=RegisterForEvent(
                        unit_of_work=lambda: InMemoryRegistrationUnitOfWork(self.store),
                        clock=FixedClock(NOW),
                    ),
                    list_registrations=ListMyRegistrations(
                        reader=InMemoryRegistrationReader(self.views)
                    ),
                )
            ],
        )
        self.client = TestClient(app)

    def headers(
        self, actor: str = "athlete", user: UUID | None = None, **extra: str
    ) -> dict[str, str]:
        return {"Authorization": f"Bearer {token(user or self.athlete_id, actor)}", **extra}

    def test_register_contract(self) -> None:
        """CA1, CA4 y CA6 por HTTP."""
        key = str(uuid4())
        created = self.client.post(
            f"/v1/events/{self.event_id}/registrations",
            headers=self.headers(**{"Idempotency-Key": key}),
        )
        replay = self.client.post(
            f"/v1/events/{self.event_id}/registrations",
            headers=self.headers(**{"Idempotency-Key": key}),
        )
        again = self.client.post(
            f"/v1/events/{self.event_id}/registrations",
            headers=self.headers(**{"Idempotency-Key": str(uuid4())}),
        )

        self.assertEqual(created.status_code, 201, created.text)
        body = created.json()
        self.assertEqual(
            created.headers["Location"], f"/v1/me/event-registrations/{body['registration_id']}"
        )
        self.assertEqual(
            set(body),
            {
                "registration_id",
                "event_id",
                "status",
                "registered_at",
                "applied_fee",
                "version",
                "event",
            },
        )
        self.assertEqual(
            (body["status"], body["version"], body["applied_fee"]), ("REGISTERED", 1, None)
        )
        self.assertEqual(body["event"]["time_zone"], "America/Bogota")
        self.assertNotIn("athlete_id", created.text)
        self.assertEqual((replay.status_code, replay.json()), (201, body))
        self.assertEqual(replay.headers["Idempotent-Replayed"], "true")
        self.assertEqual(
            (again.status_code, again.json()["code"], again.json()["details"]),
            (
                409,
                "event_registration_exists",
                [{"field": "registration_id", "issue": body["registration_id"]}],
            ),
        )

    def test_register_errors(self) -> None:
        """CA2, CA5 y CA9; clave ausente o inválida."""
        missing = self.client.post(
            f"/v1/events/{uuid4()}/registrations",
            headers=self.headers(**{"Idempotency-Key": str(uuid4())}),
        )
        self.client.post(
            f"/v1/events/{self.event_id}/registrations",
            headers=self.headers(**{"Idempotency-Key": str(uuid4())}),
        )
        full = self.client.post(
            f"/v1/events/{self.event_id}/registrations",
            headers=self.headers(user=uuid4(), **{"Idempotency-Key": str(uuid4())}),
        )
        organizer = self.client.post(
            f"/v1/events/{self.event_id}/registrations", headers=self.headers("organizer")
        )
        no_key = self.client.post(
            f"/v1/events/{self.event_id}/registrations", headers=self.headers()
        )
        bad_key = self.client.post(
            f"/v1/events/{self.event_id}/registrations",
            headers=self.headers(**{"Idempotency-Key": "x"}),
        )

        self.assertEqual((missing.status_code, missing.json()["code"]), (404, "event_not_found"))
        self.assertEqual((full.status_code, full.json()["code"]), (409, "event_full"))
        self.assertEqual(
            (organizer.status_code, organizer.json()["code"]), (403, "permission_required")
        )
        self.assertEqual(
            no_key.json()["details"], [{"field": "Idempotency-Key", "issue": "required"}]
        )
        self.assertEqual(
            bad_key.json()["details"], [{"field": "Idempotency-Key", "issue": "unknown_value"}]
        )

    def test_event_detail(self) -> None:
        """CA2: detalle con `my_registration`; inexistente → 404."""
        mine = self.views[0].registration
        detail = self.client.get(f"/v1/events/{mine.event_id}", headers=self.headers())
        free = self.client.get(f"/v1/events/{self.event_id}", headers=self.headers())
        other = self.client.get(f"/v1/events/{mine.event_id}", headers=self.headers(user=uuid4()))
        unknown = self.client.get(f"/v1/events/{uuid4()}", headers=self.headers())
        organizer = self.client.get(
            f"/v1/events/{mine.event_id}", headers=self.headers("organizer", user=mine.athlete_id)
        )

        self.assertEqual(detail.status_code, 200, detail.text)
        self.assertEqual(
            detail.json()["my_registration"]["registration_id"], str(mine.registration_id)
        )
        self.assertEqual((detail.json()["fee"], detail.json()["capacity"]), (None, 50))
        self.assertNotIn("organizer_id", detail.text)
        self.assertIsNone(free.json()["my_registration"])
        self.assertIsNone(other.json()["my_registration"], "la inscripción ajena no se revela")
        self.assertEqual((unknown.status_code, unknown.json()["code"]), (404, "event_not_found"))
        self.assertEqual(organizer.status_code, 200, "ADR-033: publicado visible a todo actor")
        self.assertIsNone(organizer.json()["my_registration"], "solo el deportista la recibe")

    def test_my_registrations(self) -> None:
        """CA8 y CA9."""
        own = self.client.get("/v1/me/event-registrations", headers=self.headers())
        empty = self.client.get("/v1/me/event-registrations", headers=self.headers(user=uuid4()))
        invalid = self.client.get(
            "/v1/me/event-registrations?status=CANCELLED", headers=self.headers()
        )
        organizer = self.client.get("/v1/me/event-registrations", headers=self.headers("organizer"))

        self.assertEqual(len(own.json()["items"]), 1)
        self.assertEqual(own.json()["items"][0]["event"]["name"], "Carrera 10K Bogotá")
        self.assertEqual(empty.json(), {"items": [], "next_cursor": None})
        self.assertEqual(
            invalid.json()["details"], [{"field": "query.status", "issue": "unknown_value"}]
        )
        self.assertEqual(organizer.status_code, 403)


class RegistrationQueriesTest(unittest.TestCase):
    def test_cursor_and_validation(self) -> None:
        athlete = Caller(uuid4(), "athlete")
        views = [
            RegistrationView(registration(athlete.user_id), summary(NOW + timedelta(days=day)))
            for day in (3, 1, 2, 2)
        ]
        query = ListMyRegistrations(reader=InMemoryRegistrationReader(views))
        seen: list[UUID] = []
        cursor: str | None = None
        while True:
            page = query(athlete, RegistrationsFilters(limit=1, cursor=cursor))
            seen.extend(item.registration.registration_id for item in page.items)
            cursor = page.next_cursor
            if cursor is None:
                break

        self.assertEqual(len(seen), 4)
        self.assertEqual(len(set(seen)), 4)
        for filters, field in (
            (RegistrationsFilters(limit=101), "limit"),
            (RegistrationsFilters(cursor="x"), "cursor"),
        ):
            with self.subTest(field=field), self.assertRaises(InvalidRegistrationsQuery):
                query(athlete, filters)


def message(registration_item: EventRegistration, **overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "event_id": str(uuid4()),
        "event_type": "usuario.inscrito.v1",
        "occurred_at": "2026-10-12T21:00:00Z",
        "producer": "events-management",
        "aggregate_type": "event_registration",
        "aggregate_id": str(registration_item.registration_id),
        "aggregate_version": 1,
        "correlation_id": None,
        "causation_id": str(uuid4()),
        "schema_version": 1,
        "payload": registered_payload(registration_item),
    }
    values.update(overrides)
    return values


class RegistrationProjectionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.read = ReadStore()
        self.project = EventsProjector(
            unit_of_work=lambda: InMemoryProjectionUnitOfWork(self.read), clock=FixedClock()
        )

    def test_projected_once_and_conflicting_version_rejected(self) -> None:
        """CA7: duplicado y republicación sin efectos; otra versión → sin ACK (DLQ)."""
        item = registration(uuid4())
        first = parse_sport_event_message(message(item))
        assert isinstance(first, RegistrationCreatedMessage)

        self.assertEqual(self.project(first), ProjectionOutcome.PROJECTED)
        self.assertEqual(self.project(first), ProjectionOutcome.DUPLICATE)
        self.assertEqual(
            self.project(parse_sport_event_message(message(item))), ProjectionOutcome.STALE
        )
        conflicting = dict(message(item, aggregate_version=2))
        conflicting["payload"] = {**conflicting["payload"], "version": 2}
        with self.assertRaises(ProjectionConflict):
            self.project(parse_sport_event_message(conflicting))
        self.assertEqual(list(self.read.registrations), [item.registration_id])


if __name__ == "__main__":
    unittest.main()
