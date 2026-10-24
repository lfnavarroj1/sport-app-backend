"""HU026 (ADR-041): comandos con dobles en memoria (carrera de la clave de idempotencia y
auditoría), lectura validada de `ruta.publicada.v1`/`ruta.retirada.v1` y su proyección en
`route_view` (duplicados, orden inverso, reconstrucción y rechazo hacia la DLQ)."""

import json
import unittest
from datetime import timedelta
from typing import Any
from uuid import uuid4

from fakes import FakePermissions, FixedClock, InMemoryProjectionUnitOfWork, InMemoryReadStore
from route_fakes import AUTHOR, InMemoryRouteStore, InMemoryRouteUnitOfWork, route_input

from training_management.application.commands.routes import (
    PublishRoute,
    PublishRouteCommand,
    RetireRoute,
    RetireRouteCommand,
)
from training_management.application.errors import PermissionRequired
from training_management.application.messages import (
    InvalidEvent,
    RouteChangedEvent,
    parse_training_event,
)
from training_management.application.ports import Caller
from training_management.application.projections.plan_projection import ProjectionOutcome
from training_management.application.projections.snapshot_projection import TrainingProjector
from training_management.domain.routes.model import RouteStatus
from training_management.infrastructure.persistence_write.outbox import ClaimedEvent
from training_management.workers.outbox_publisher import build_envelope
from training_management.workers.read_projector import ProjectionMessageHandler


class _Message:
    def __init__(self, envelope: dict[str, Any]) -> None:
        self.data = json.dumps(envelope).encode()
        self.message_id = "m-1"
        self.delivery_attempt = 1
        self.acked = self.nacked = False

    def ack(self) -> None:
        self.acked = True

    def nack(self) -> None:
        self.nacked = True


class RouteFlowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = InMemoryRouteStore()
        self.clock = FixedClock()
        self.organizer = Caller(uuid4(), "organizer")
        self.hook: Any = None
        self.calls, self.hook_on_call = 0, 0
        permissions = FakePermissions({"organizer": {AUTHOR}})
        self.publish = PublishRoute(
            unit_of_work=self.unit_of_work, permissions=permissions, clock=self.clock
        )
        self.retire = RetireRoute(
            unit_of_work=self.unit_of_work, permissions=permissions, clock=self.clock
        )
        self.read = InMemoryReadStore()
        self.project = TrainingProjector(
            unit_of_work=lambda: InMemoryProjectionUnitOfWork(self.read), clock=self.clock
        )

    def unit_of_work(self) -> InMemoryRouteUnitOfWork:
        self.calls += 1
        hook = self.hook if self.calls == self.hook_on_call else None
        return InMemoryRouteUnitOfWork(self.store, before_commit=hook)

    def envelopes(self) -> list[dict[str, Any]]:
        return [
            json.loads(
                json.dumps(
                    build_envelope(
                        ClaimedEvent(
                            event_id=message.event_id,
                            event_type=message.event_type,
                            schema_version=message.schema_version,
                            aggregate_type=message.aggregate_type,
                            aggregate_id=message.aggregate_id,
                            aggregate_version=message.aggregate_version,
                            occurred_at=message.occurred_at,
                            correlation_id=message.correlation_id,
                            causation_id=message.causation_id,
                            payload=message.payload,
                            attempts=0,
                        )
                    )
                )
            )
            for message in self.store.outbox
        ]

    def publish_and_retire(self) -> None:
        key = uuid4()
        route = self.publish(self.organizer, PublishRouteCommand(route_input(), key)).route
        self.clock.advance(timedelta(hours=1))
        self.retire(self.organizer, RetireRouteCommand(route.route_id, 1))

    def test_concurrent_request_with_the_same_key_returns_the_winner(self) -> None:
        """Si otra solicitud con la misma clave confirma primero, se devuelve su resultado."""
        command = PublishRouteCommand(route_input(), uuid4())
        rival = PublishRoute(
            unit_of_work=lambda: InMemoryRouteUnitOfWork(self.store),
            permissions=FakePermissions({"organizer": {AUTHOR}}),
            clock=self.clock,
        )
        winner: list[Any] = []
        # La primera unidad solo busca la clave; la segunda confirma y choca con la rival.
        self.hook_on_call = 2
        self.hook = lambda: winner.append(rival(self.organizer, command))

        result = self.publish(self.organizer, command)

        self.assertTrue(result.replayed)
        self.assertEqual(result.route.route_id, winner[0].route.route_id)
        self.assertEqual(len(self.store.routes), 1)
        self.assertEqual(len(self.store.outbox), 1)

    def test_rejections_are_audited_without_route_data(self) -> None:
        with (
            self.assertLogs("sportapp.audit", level="INFO") as audit,
            self.assertRaises(PermissionRequired),
        ):
            self.publish(Caller(uuid4(), "athlete"), PublishRouteCommand(route_input(), uuid4()))

        [record] = audit.records
        self.assertEqual(record.fields["result"], "rejected")  # type: ignore[attr-defined]
        self.assertEqual(record.fields["reason"], "permission_required")  # type: ignore[attr-defined]
        self.assertNotIn("Vuelta", " ".join(audit.output))

    def test_events_project_full_state_and_ignore_duplicates(self) -> None:
        self.publish_and_retire()
        published, retired = (parse_training_event(item) for item in self.envelopes())
        assert isinstance(published, RouteChangedEvent)
        assert isinstance(retired, RouteChangedEvent)

        self.assertEqual(self.project(published), ProjectionOutcome.PROJECTED)
        self.assertEqual(self.project(published), ProjectionOutcome.DUPLICATE)
        self.assertEqual(self.read.routes[published.route.route_id].status, RouteStatus.PUBLISHED)
        self.assertEqual(self.project(retired), ProjectionOutcome.PROJECTED)
        self.assertEqual(self.read.routes[retired.route.route_id].status, RouteStatus.RETIRED)
        self.assertEqual(self.read.routes[retired.route.route_id], retired.route)

    def test_retire_arriving_first_wins_and_rebuild_reaches_the_same_view(self) -> None:
        """Sin orden global: un retiro anterior a su publicación no se deshace."""
        self.publish_and_retire()
        published, retired = (parse_training_event(item) for item in self.envelopes())

        self.assertEqual(self.project(retired), ProjectionOutcome.PROJECTED)
        self.assertEqual(self.project(published), ProjectionOutcome.STALE)
        expected = dict(self.read.routes)
        rebuilt = InMemoryReadStore()
        project = TrainingProjector(
            unit_of_work=lambda: InMemoryProjectionUnitOfWork(rebuilt), clock=self.clock
        )
        for event in (published, retired):
            project(event)
        self.assertEqual(rebuilt.routes, expected)

    def test_contract_violations_go_to_the_dlq(self) -> None:
        self.publish_and_retire()
        published, retired = self.envelopes()
        cases = {
            "aggregate_id_mismatch": {**published, "aggregate_id": str(uuid4())},
            "route_version_mismatch": {**published, "aggregate_version": 2},
            "status_mismatch": {**retired, "event_type": "ruta.publicada.v1"},
            "unsupported_schema_version": {**published, "schema_version": 2},
            "distance_km_invalid": {
                **published,
                "payload": {**published["payload"], "distance_km": "0.0"},
            },
        }
        for reason, envelope in cases.items():
            with self.subTest(reason=reason):
                with self.assertRaises(InvalidEvent) as error:
                    parse_training_event(envelope)
                self.assertEqual(error.exception.reason, reason)
                message = _Message(envelope)
                with self.assertLogs("sportapp.projector", level="WARNING"):
                    ProjectionMessageHandler(self.project)(message)
                self.assertEqual((message.acked, message.nacked), (False, True))
        self.assertEqual(self.read.routes, {})


if __name__ == "__main__":
    unittest.main()
