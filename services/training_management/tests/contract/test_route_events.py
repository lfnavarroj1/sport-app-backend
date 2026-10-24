"""HU026: los sobres que publica el servicio cumplen `ruta.publicada.v1` y `ruta.retirada.v1`
(ADR-041) y el proyector acepta lo que publica el productor."""

import json
import unittest
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from sportapp_platform.testing.contracts import repository_root
from training_management.application.messages import RouteChangedEvent, parse_training_event
from training_management.domain.plans.model import Sport
from training_management.domain.routes.events import (
    AGGREGATE_TYPE,
    EVENT_TYPE_BY_STATUS,
    PUBLISHED_EVENT_TYPE,
    RETIRED_EVENT_TYPE,
    SCHEMA_VERSION,
    route_document,
)
from training_management.domain.routes.model import (
    Difficulty,
    GeoPoint,
    Route,
    RouteInput,
)
from training_management.infrastructure.persistence_write.outbox import ClaimedEvent
from training_management.workers.outbox_publisher import build_envelope

SCHEMAS = repository_root(Path(__file__).resolve()) / "contracts" / "events" / "entrenamientos"
NOW = datetime(2026, 10, 24, 15, 30, tzinfo=UTC)


def validator(event_type: str) -> Draft202012Validator:
    schema = json.loads((SCHEMAS / f"{event_type}.schema.json").read_text(encoding="utf-8"))
    return Draft202012Validator(schema, format_checker=FormatChecker())


def route() -> Route:
    return Route.publish(
        route_id=uuid.uuid4(),
        organizer_id=uuid.uuid4(),
        data=RouteInput(
            name="Vuelta al embalse",
            description=None,
            sport=Sport.CYCLING,
            distance_km=Decimal("42"),
            difficulty=Difficulty.HIGH,
            city="Medellín",
            country_code="CO",
            start_point=GeoPoint(lat=6.2442, lng=-75.5812),
            waypoints=(GeoPoint(lat=6.3, lng=-75.5),),
        ),
        now=NOW,
    )


def published(state: Route) -> dict[str, Any]:
    envelope = build_envelope(
        ClaimedEvent(
            event_id=uuid.uuid4(),
            event_type=EVENT_TYPE_BY_STATUS[state.status],
            schema_version=SCHEMA_VERSION,
            aggregate_type=AGGREGATE_TYPE,
            aggregate_id=state.route_id,
            aggregate_version=state.version,
            occurred_at=state.updated_at,
            correlation_id=uuid.uuid4(),
            causation_id=str(uuid.uuid4()),
            payload=route_document(state),
            attempts=0,
        )
    )
    # Ida y vuelta por JSON: así viaja por Pub/Sub.
    loaded: dict[str, Any] = json.loads(json.dumps(envelope))
    return loaded


class RouteEventsContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.route = route()
        self.retired = self.route.retire(NOW + timedelta(days=1))

    def test_published_and_retired_envelopes_match_their_schemas(self) -> None:
        for state, event_type, version in (
            (self.route, PUBLISHED_EVENT_TYPE, 1),
            (self.retired, RETIRED_EVENT_TYPE, 2),
        ):
            with self.subTest(event_type=event_type):
                body = published(state)

                validator(event_type).validate(body)
                self.assertEqual(
                    (body["event_type"], body["aggregate_type"], body["aggregate_version"]),
                    (event_type, "route", version),
                )
                self.assertEqual(body["aggregate_id"], body["payload"]["route_id"])
                self.assertEqual(body["payload"]["distance_km"], "42.0")
                self.assertTrue(body["payload"]["published_at"].endswith("Z"))

    def test_each_schema_rejects_the_other_state(self) -> None:
        self.assertFalse(validator(PUBLISHED_EVENT_TYPE).is_valid(published(self.retired)))
        self.assertFalse(validator(RETIRED_EVENT_TYPE).is_valid(published(self.route)))

    def test_schema_rejects_personal_data_and_malformed_values(self) -> None:
        check = validator(PUBLISHED_EVENT_TYPE)
        for field, value in (
            ("organizer_name", "Ana"),
            ("organizer_email", "ana@example.com"),
            ("gpx", "<gpx/>"),
            ("distance_km", 42),
            ("distance_km", "42.25"),
            ("start_point", {"lat": 91, "lng": 0}),
            ("country_code", "col"),
            ("source", "ATHLETE"),
        ):
            body = published(self.route)
            body["payload"][field] = value
            with self.subTest(field=field, value=value):
                self.assertFalse(check.is_valid(body))

    def test_projector_accepts_what_the_producer_publishes(self) -> None:
        for state in (self.route, self.retired):
            with self.subTest(status=state.status):
                event = parse_training_event(published(state))

                assert isinstance(event, RouteChangedEvent)
                self.assertEqual(event.route, state)


if __name__ == "__main__":
    unittest.main()
