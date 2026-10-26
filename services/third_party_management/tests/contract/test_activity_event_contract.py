"""HU031 (ADR-042 §4): lo que publica `terceros-outbox-publisher` cumple
`contracts/events/terceros/actividad.importada.v1.schema.json`.

El sobre se arma con el mismo camino que en producción: `activity_event` (application/imports)
produce el `OutboxMessage` que se confirma con la actividad; la outbox lo devuelve como
`ClaimedEvent` y `build_envelope` arma el mensaje. Los casos negativos comprueban que el
esquema rechaza lo que el contrato excluye, y que el payload nunca lleva tokens ni campos del
JSON de Strava.
"""

import copy
import json
import secrets
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from jsonschema import Draft202012Validator, FormatChecker

from sportapp_platform.testing.contracts import repository_root
from third_party_management.application.imports import activity_event
from third_party_management.application.ports import OutboxMessage
from third_party_management.domain.activities import next_import, normalize_activity
from third_party_management.domain.synchronization import SyncRun
from third_party_management.infrastructure.persistence.outbox import ClaimedEvent
from third_party_management.testing.strava_simulator import activity
from third_party_management.workers.outbox_publisher import build_envelope

SCHEMA = (
    repository_root(Path(__file__).resolve())
    / "contracts"
    / "events"
    / "terceros"
    / "actividad.importada.v1.schema.json"
)
NOW = datetime(2026, 10, 25, 15, 0, tzinfo=UTC)
# Campos del JSON de Strava que nunca salen del servicio.
STRAVA_ONLY_FIELDS = ("athlete", "map", "type", "sport_type", "start_date", "elapsed_time")


def record(**overrides: Any) -> Any:
    """Actividad sintética del simulador (id 9876543210, ciclismo de 90 minutos)."""
    base = activity(
        9876543210,
        start_date="2026-10-20T11:00:00Z",
        sport_type="Ride",
        elapsed_time=5400,
        distance=40500.4,
        name="Ruta sintética",
    )
    base.update(overrides)
    return base


def envelope_for(source: Any, *, correlated: bool = True) -> tuple[OutboxMessage, dict[str, Any]]:
    user_id = uuid4()
    normalized = normalize_activity(source)
    imported = next_import(None, normalized, user_id, NOW)
    if imported is None:
        raise AssertionError("una actividad nueva siempre se importa")
    run = SyncRun.queue(user_id, NOW, uuid4() if correlated else None)
    message = activity_event(normalized, imported, run, NOW)
    event = ClaimedEvent(**message.__dict__, attempts=0)
    return message, build_envelope(event)


class ActivityImportedEventContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        cls.validator = Draft202012Validator(cls.schema, format_checker=FormatChecker())

    def assert_valid(self, envelope: dict[str, Any]) -> None:
        errors = [error.message for error in self.validator.iter_errors(envelope)]
        self.assertEqual(errors, [])

    def assert_invalid(self, envelope: dict[str, Any]) -> None:
        self.assertFalse(self.validator.is_valid(envelope), json.dumps(envelope, default=str))

    def test_schema_is_a_valid_draft_2020_12_schema(self) -> None:
        Draft202012Validator.check_schema(self.schema)

    def test_published_envelope_matches_the_schema(self) -> None:
        message, envelope = envelope_for(record())

        self.assert_valid(envelope)
        self.assertEqual(envelope["event_type"], "actividad.importada.v1")
        self.assertEqual(envelope["aggregate_id"], envelope["payload"]["activity_id"])
        self.assertEqual(envelope["aggregate_version"], envelope["payload"]["version"])
        self.assertEqual(envelope["causation_id"], message.causation_id)
        self.assertEqual(envelope["payload"]["distance_meters"], 40500)
        self.assertEqual(envelope["payload"]["started_at"], "2026-10-20T11:00:00Z")
        self.assertEqual(envelope["payload"]["ended_at"], "2026-10-20T12:30:00Z")

    def test_optional_values_and_missing_correlation_are_valid(self) -> None:
        source = record(name=None, moving_time=None, total_elevation_gain=None, timezone=None)

        _, envelope = envelope_for(source, correlated=False)

        self.assert_valid(envelope)
        self.assertIsNone(envelope["correlation_id"])
        self.assertIsNone(envelope["payload"]["elevation_gain_meters"])

    def test_every_sport_mapping_is_valid(self) -> None:
        for sport_type in ("Run", "Ride", "Swim", "WeightTraining", "Yoga"):
            with self.subTest(sport_type=sport_type):
                _, envelope = envelope_for(record(sport_type=sport_type, type=sport_type))

                self.assert_valid(envelope)

    def test_a_new_version_is_still_valid(self) -> None:
        user_id = uuid4()
        first = normalize_activity(record())
        changed = normalize_activity(record(elapsed_time=6000))
        imported = next_import(None, first, user_id, NOW)
        updated = next_import(imported, changed, user_id, NOW + timedelta(hours=1))
        if updated is None:
            self.fail("un hash distinto produce la versión siguiente")
        run = SyncRun.queue(user_id, NOW, None)

        message = activity_event(changed, updated, run, NOW)
        envelope = build_envelope(ClaimedEvent(**message.__dict__, attempts=0))

        self.assert_valid(envelope)
        self.assertEqual(envelope["aggregate_version"], 2)

    def test_schema_rejects_shapes_outside_the_contract(self) -> None:
        _, valid = envelope_for(record())
        cases: dict[str, Any] = {
            "campo de más en el sobre": lambda e: e.update(extra="x"),
            "campo de más en el payload": lambda e: e["payload"].update(raw={"id": 1}),
            "falta event_id": lambda e: e.pop("event_id"),
            "falta payload.user_id": lambda e: e["payload"].pop("user_id"),
            "aggregate_version 0": lambda e: e.update(aggregate_version=0),
            "payload.version 0": lambda e: e["payload"].update(version=0),
            "occurred_at sin Z": lambda e: e.update(occurred_at="2026-10-25T15:00:00+00:00"),
            "started_at sin Z": lambda e: e["payload"].update(started_at="2026-10-20T11:00:00"),
            "otro productor": lambda e: e.update(producer="training-management"),
            "otro tipo": lambda e: e.update(event_type="actividad.importada.v2"),
            "deporte desconocido": lambda e: e["payload"].update(sport="Ride"),
            "external_activity_id no numérico": lambda e: e["payload"].update(
                external_activity_id="abc"
            ),
            "causation_id no UUID": lambda e: e.update(causation_id="sync-1"),
        }
        for name, mutate in cases.items():
            with self.subTest(case=name):
                envelope = copy.deepcopy(valid)
                mutate(envelope)

                self.assert_invalid(envelope)

    def test_payload_never_carries_tokens_nor_strava_fields(self) -> None:
        access = secrets.token_urlsafe(24)
        source = record(access_token=access, athlete={"id": 1, "firstname": "Sintético"})

        _, envelope = envelope_for(source)

        text = json.dumps(envelope)
        self.assertNotIn(access, text)
        self.assertNotIn("token", text)
        for name in STRAVA_ONLY_FIELDS:
            with self.subTest(field=name):
                self.assertNotIn(name, envelope["payload"])
        self.assertEqual(
            set(envelope["payload"]), set(self.schema["properties"]["payload"]["required"])
        )


if __name__ == "__main__":
    unittest.main()
