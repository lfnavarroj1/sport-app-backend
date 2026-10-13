"""Workers y adaptadores de HU021 sin infraestructura: mensajes, proyector, outbox y
política de permisos (CA5)."""

import json
import os
import threading
import unittest
import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any
from unittest import mock

from fakes import NOW, FixedClock, InMemoryProjectionUnitOfWork, ReadStore, event_body

from events_management.api.payload import parse_event
from events_management.application.messages import (
    InvalidEvent,
    UnsupportedEvent,
    parse_sport_event_message,
)
from events_management.application.projections.event_projection import (
    ProjectEventCreated,
    ProjectionOutcome,
)
from events_management.domain.sport_events.created import event_document
from events_management.domain.sport_events.model import SportEvent, validate_event
from events_management.infrastructure.authorization import ContractPermissionPolicy
from events_management.infrastructure.persistence_write.outbox import ClaimedEvent
from events_management.settings import OutboxPublisherSettings
from events_management.workers.outbox_publisher import OutboxRelay, build_envelope
from events_management.workers.pubsub_setup import ensure_resources
from events_management.workers.read_projector import ProjectionMessageHandler
from sportapp_platform.authorization import load_actor_permissions, resolve_actor_permissions_path
from sportapp_platform.config import ConfigurationError


def an_event() -> SportEvent:
    return SportEvent.draft(
        event_id=uuid.uuid4(),
        organizer_id=uuid.uuid4(),
        details=validate_event(parse_event(event_body()), NOW),
        now=NOW,
    )


def envelope(event: SportEvent | None = None, **overrides: Any) -> dict[str, Any]:
    event = event or an_event()
    values: dict[str, Any] = {
        "event_id": str(uuid.uuid4()),
        "event_type": "evento.creado.v1",
        "occurred_at": "2026-10-12T21:00:00Z",
        "producer": "events-management",
        "aggregate_type": "event",
        "aggregate_id": str(event.event_id),
        "aggregate_version": 1,
        "correlation_id": str(uuid.uuid4()),
        "causation_id": str(uuid.uuid4()),
        "schema_version": 1,
        "payload": event_document(event),
    }
    values.update(overrides)
    return values


@dataclass
class Message:
    body: dict[str, Any] | bytes
    message_id: str = "m-1"
    delivery_attempt: int | None = 1
    acked: bool = False
    nacked: bool = False

    @property
    def data(self) -> bytes:
        return self.body if isinstance(self.body, bytes) else json.dumps(self.body).encode()

    def ack(self) -> None:
        self.acked = True

    def nack(self) -> None:
        self.nacked = True


class ParseSportEventMessageTest(unittest.TestCase):
    def test_valid_event_round_trips(self) -> None:
        event = an_event()

        message = parse_sport_event_message(envelope(event))

        self.assertEqual(message.event, event)
        self.assertEqual(message.aggregate_version, 1)

    def test_other_event_types_are_unsupported(self) -> None:
        with self.assertRaises(UnsupportedEvent):
            parse_sport_event_message(envelope(event_type="evento.publicado.v1"))

    def test_contract_violations(self) -> None:
        event = an_event()
        payload = event_document(event)
        cases: dict[str, Any] = {
            "envelope_not_object": [],
            "event_type_missing": envelope(event_type=None),
            "unsupported_schema_version": envelope(schema_version=2),
            "aggregate_version_invalid": envelope(aggregate_version=0),
            "payload_not_object": envelope(payload="x"),
            "aggregate_id_mismatch": envelope(event, aggregate_id=str(uuid.uuid4())),
            "version_mismatch": envelope(event, aggregate_version=2),
            "event_id_not_uuid": envelope(event_id="x"),
            "correlation_id_not_uuid": envelope(event, correlation_id="x"),
            "unknown_status": envelope(event, payload={**payload, "status": "PUBLISHED"}),
            "time_zone_invalid": envelope(event, payload={**payload, "time_zone": ""}),
            "name_invalid": envelope(event, payload={**payload, "name": " untrimmed "}),
            "capacity_out_of_range": envelope(event, payload={**payload, "capacity": 0}),
            "starts_at_not_utc": envelope(
                event, payload={**payload, "starts_at": "2026-11-15T07:00:00-05:00"}
            ),
            "ends_at_not_datetime": envelope(event, payload={**payload, "ends_at": "x"}),
            "ends_at_mismatch": envelope(
                event, payload={**payload, "ends_at": "2026-11-15T16:00:00Z"}
            ),
            "location_not_object": envelope(event, payload={**payload, "location": "x"}),
            "country_code_invalid": envelope(
                event,
                payload={**payload, "location": {**payload["location"], "country_code": "ZZ"}},
            ),
        }
        for reason, body in cases.items():
            with self.subTest(reason=reason), self.assertRaises(InvalidEvent) as raised:
                parse_sport_event_message(body)
            self.assertEqual(raised.exception.reason, reason)


class ProjectEventCreatedTest(unittest.TestCase):
    def setUp(self) -> None:
        self.read = ReadStore()
        self.project = ProjectEventCreated(
            unit_of_work=lambda: InMemoryProjectionUnitOfWork(self.read), clock=FixedClock()
        )

    def test_projects_once_and_registers_duplicates(self) -> None:
        """CA5: una entrega repetida (mismo mensaje) no vuelve a proyectar."""
        message = parse_sport_event_message(envelope())

        self.assertEqual(self.project(message), ProjectionOutcome.PROJECTED)
        self.assertEqual(self.project(message), ProjectionOutcome.DUPLICATE)
        self.assertEqual(self.read.views, {message.event.event_id: message.event})

    def test_republished_event_with_new_message_id_is_stale(self) -> None:
        """La outbox puede republicar tras un fallo sin ACK: nunca duplica la vista."""
        event = an_event()
        first = parse_sport_event_message(envelope(event))
        again = parse_sport_event_message(envelope(event))

        self.project(first)

        self.assertEqual(self.project(again), ProjectionOutcome.STALE)
        self.assertEqual(len(self.read.views), 1)
        self.assertEqual(len(self.read.processed), 2)

    def test_concurrent_delivery_loses_to_the_first_commit(self) -> None:
        message = parse_sport_event_message(envelope())
        rival = ProjectEventCreated(
            unit_of_work=lambda: InMemoryProjectionUnitOfWork(self.read), clock=FixedClock()
        )
        racing = ProjectEventCreated(
            unit_of_work=lambda: InMemoryProjectionUnitOfWork(
                self.read, before_commit=lambda: rival(message)
            ),
            clock=FixedClock(),
        )

        self.assertEqual(racing(message), ProjectionOutcome.DUPLICATE)
        self.assertEqual(len(self.read.views), 1)


class ProjectionMessageHandlerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.read = ReadStore()
        self.handler = ProjectionMessageHandler(
            ProjectEventCreated(
                unit_of_work=lambda: InMemoryProjectionUnitOfWork(self.read), clock=FixedClock()
            )
        )

    def test_acks_after_projecting_and_on_duplicate(self) -> None:
        body = envelope()
        first, second = Message(body), Message(body)

        self.handler(first)
        self.handler(second)

        self.assertTrue(first.acked and second.acked)
        self.assertEqual(len(self.read.views), 1)

    def test_invalid_messages_are_not_acked(self) -> None:
        for body in (b"no-json", envelope(schema_version=9)):
            message = Message(body)
            with self.subTest(body=str(body)[:20]):
                with self.assertLogs("sportapp.projector", level="WARNING"):
                    self.handler(message)
                self.assertEqual((message.acked, message.nacked), (False, True))

    def test_unsupported_types_are_acked_without_effects(self) -> None:
        message = Message(envelope(event_type="evento.publicado.v1"))

        self.handler(message)

        self.assertTrue(message.acked)
        self.assertEqual(self.read.views, {})

    def test_version_gap_is_not_acked(self) -> None:
        event = an_event()
        later = SportEvent(**{**event.__dict__, "version": 2})
        gap = Message(envelope(later, aggregate_version=2))

        with self.assertLogs("sportapp.projector", level="WARNING") as logs:
            self.handler(gap)

        self.assertEqual((gap.acked, gap.nacked), (False, True))
        self.assertIn("projector.version_gap", logs.output[0])

    def test_failure_is_not_acked_and_does_not_leak_content(self) -> None:
        def broken(_: Any) -> ProjectionOutcome:
            raise RuntimeError("base de lectura caída")

        message = Message(envelope())
        with self.assertLogs("sportapp.projector", level="WARNING") as logs:
            ProjectionMessageHandler(broken)(message)

        self.assertEqual((message.acked, message.nacked), (False, True))
        self.assertNotIn("Carrera", str(logs.records[0].fields))  # type: ignore[attr-defined]


@dataclass
class FakeOutboxStore:
    events: list[ClaimedEvent]
    published: list[uuid.UUID] = field(default_factory=list)
    failed: list[tuple[uuid.UUID, str, float]] = field(default_factory=list)

    def claim_batch(self, worker_id: str, batch_size: int, lease_seconds: float) -> list[Any]:
        claimed, self.events = self.events[:batch_size], self.events[batch_size:]
        return claimed

    def mark_published(self, event_id: uuid.UUID, worker_id: str) -> None:
        self.published.append(event_id)

    def mark_failed(
        self, event_id: uuid.UUID, worker_id: str, error: str, retry_in_seconds: float
    ) -> None:
        self.failed.append((event_id, error, retry_in_seconds))


class OutboxRelayTest(unittest.TestCase):
    def claimed(self, attempts: int = 0) -> ClaimedEvent:
        event = an_event()
        return ClaimedEvent(
            event_id=uuid.uuid4(),
            event_type="evento.creado.v1",
            schema_version=1,
            aggregate_type="event",
            aggregate_id=event.event_id,
            aggregate_version=1,
            occurred_at=NOW + timedelta(hours=2),
            correlation_id=None,
            causation_id=str(uuid.uuid4()),
            payload=event_document(event),
            attempts=attempts,
        )

    def settings(self, **overrides: Any) -> OutboxPublisherSettings:
        values: dict[str, Any] = {
            "app_env": "test",
            "database_write_url": "postgresql+psycopg://contract@127.0.0.1:1/contract",
            "pubsub_project_id": "p",
            "worker_id": "w-1",
        }
        values.update(overrides)
        return OutboxPublisherSettings(**values)

    def test_envelope_and_ordering_key(self) -> None:
        event = self.claimed()
        sent: list[tuple[dict[str, Any], str]] = []

        class Publisher:
            def publish(self, body: dict[str, Any], ordering_key: str) -> None:
                sent.append((body, ordering_key))

        store = FakeOutboxStore([event])
        published = OutboxRelay(store, Publisher(), self.settings()).run_once()

        self.assertEqual(published, 1)
        self.assertEqual(store.published, [event.event_id])
        body, key = sent[0]
        self.assertEqual(key, str(event.aggregate_id))
        self.assertEqual(body, build_envelope(event))
        self.assertEqual(body["producer"], "events-management")
        self.assertEqual(body["occurred_at"], "2026-10-12T23:00:00Z")

    def test_failure_keeps_event_pending_with_bounded_backoff(self) -> None:
        class Failing:
            def publish(self, body: dict[str, Any], ordering_key: str) -> None:
                raise RuntimeError("sin ACK")

        store = FakeOutboxStore([self.claimed(attempts=30)])
        with self.assertLogs("sportapp.outbox", level="WARNING"):
            OutboxRelay(store, Failing(), self.settings()).run_once()

        self.assertEqual(store.published, [])
        [(_, error, delay)] = store.failed
        self.assertEqual(error, "RuntimeError")
        self.assertLessEqual(delay, 601)

    def test_run_forever_drains_backlog_and_stops(self) -> None:
        """Recuperación básica: un backlog mayor que el lote se publica en varios ciclos."""
        backlog = [self.claimed() for _ in range(5)]
        store = FakeOutboxStore(list(backlog))
        stop = threading.Event()

        class Publisher:
            def publish(self, body: dict[str, Any], ordering_key: str) -> None:
                if len(store.published) == len(backlog) - 1:
                    stop.set()

        OutboxRelay(store, Publisher(), self.settings(outbox_batch_size=2)).run_forever(stop)

        self.assertEqual(store.published, [event.event_id for event in backlog])


class ContractPermissionPolicyTest(unittest.TestCase):
    def test_reads_versioned_contract(self) -> None:
        policy = ContractPermissionPolicy(load_actor_permissions(resolve_actor_permissions_path()))

        self.assertTrue(policy.allows("organizer", "events.create"))
        self.assertFalse(policy.allows("service_provider", "events.create"))
        self.assertFalse(policy.allows("athlete", "events.create"))
        self.assertFalse(policy.allows("desconocido", "events.create"))


class PubSubSetupTest(unittest.TestCase):
    def test_refuses_to_run_outside_the_emulator(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True), self.assertRaises(ConfigurationError):
            ensure_resources("sportapp-local")


if __name__ == "__main__":
    unittest.main()
