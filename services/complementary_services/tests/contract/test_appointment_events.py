"""Los sobres de `sesion.programada.v1` y `sesion.cancelada.v1` que publica
`servicios-outbox-publisher` cumplen sus esquemas versionados (HU040, ADR-046 §5): estado
completo de la cita, `aggregate_type = appointment`, sin datos de salud ni `meeting_url`."""

import json
import unittest
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from complementary_services.application.commands.appointments import (
    AGGREGATE_TYPE,
    BOOKED_EVENT,
    CANCELLED_EVENT,
    SCHEMA_VERSION,
    appointment_payload,
)
from complementary_services.domain.appointments.appointment import Appointment, BookableSlot
from complementary_services.domain.catalog.offer import (
    BillingUnit,
    Category,
    Currency,
    Modality,
    Price,
    ServiceOffer,
    ServiceStatus,
)
from complementary_services.infrastructure.persistence.outbox import ClaimedEvent
from complementary_services.workers.outbox_publisher import build_envelope
from sportapp_platform.testing.contracts import repository_root

SCHEMAS = repository_root(Path(__file__).resolve()) / "contracts" / "events" / "servicios"
NOW = datetime(2026, 10, 12, 15, 0, tzinfo=UTC)


def booked() -> Appointment:
    service = ServiceOffer(
        service_id=uuid.uuid4(),
        provider_id=uuid.uuid4(),
        name="Entrenamiento funcional virtual",
        description="Sesión virtual sintética para pruebas de contrato.",
        category=Category.COACHING,
        modality=Modality.VIRTUAL,
        price=Price(Decimal("80000.00"), Currency.COP, BillingUnit.SESSION),
        availability=(),
        status=ServiceStatus.PUBLISHED,
        version=2,
        created_at=NOW,
        updated_at=NOW,
        published_at=NOW,
    )
    start = NOW + timedelta(days=3)
    slot = BookableSlot(
        availability_id=uuid.uuid4(),
        service_id=service.service_id,
        starts_at=start,
        ends_at=start + timedelta(hours=1),
        time_zone="America/Bogota",
        capacity=1,
        booked_count=0,
    )
    return Appointment.book(
        appointment_id=uuid.uuid4(), athlete_id=uuid.uuid4(), offer=service, slot=slot, now=NOW
    )


def envelope(appointment: Appointment, event_type: str, causation_id: str | None) -> Any:
    return json.loads(
        json.dumps(
            build_envelope(
                ClaimedEvent(
                    event_id=uuid.uuid4(),
                    event_type=event_type,
                    schema_version=SCHEMA_VERSION,
                    aggregate_type=AGGREGATE_TYPE,
                    aggregate_id=appointment.appointment_id,
                    aggregate_version=appointment.version,
                    occurred_at=NOW,
                    correlation_id=uuid.uuid4(),
                    causation_id=causation_id,
                    payload=appointment_payload(appointment),
                    attempts=0,
                )
            )
        )
    )


def validator(event_type: str) -> Draft202012Validator:
    schema = json.loads((SCHEMAS / f"{event_type}.schema.json").read_text(encoding="utf-8"))
    return Draft202012Validator(schema, format_checker=FormatChecker())


class AppointmentEventsContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.appointment = booked()
        self.booked = envelope(self.appointment, BOOKED_EVENT, str(uuid.uuid4()))
        self.cancelled = envelope(
            self.appointment.cancel(NOW + timedelta(hours=1)), CANCELLED_EVENT, None
        )

    def test_envelopes_match_their_schemas(self) -> None:
        validator(BOOKED_EVENT).validate(self.booked)
        validator(CANCELLED_EVENT).validate(self.cancelled)
        for body, status, version in (
            (self.booked, "BOOKED", 1),
            (self.cancelled, "CANCELLED_BY_ATHLETE", 2),
        ):
            with self.subTest(status=status):
                payload = body["payload"]
                self.assertEqual(payload["appointment_id"], body["aggregate_id"])
                self.assertEqual(body["aggregate_type"], "appointment")
                self.assertEqual((payload["status"], body["aggregate_version"]), (status, version))
                self.assertIsNone(payload["meeting_url"])
                self.assertEqual(payload["starts_at"], "2026-10-15T15:00:00Z")
        self.assertEqual(self.cancelled["payload"]["cancelled_at"], "2026-10-12T16:00:00Z")

    def test_schemas_reject_contract_violations(self) -> None:
        cases: dict[str, tuple[str, str | None, str, object]] = {
            "health_data": (BOOKED_EVENT, "payload", "weight_kg", 70),
            "meeting_url": (BOOKED_EVENT, "payload", "meeting_url", "https://example.invalid"),
            "local_offset": (BOOKED_EVENT, "payload", "starts_at", "2026-10-15T10:00:00-05:00"),
            "booked_cancelled_at": (BOOKED_EVENT, "payload", "cancelled_at", NOW.isoformat()),
            "booked_version": (BOOKED_EVENT, None, "aggregate_version", 2),
            "booked_without_key": (BOOKED_EVENT, None, "causation_id", None),
            "other_aggregate": (BOOKED_EVENT, None, "aggregate_type", "service"),
            "cancelled_status": (CANCELLED_EVENT, "payload", "status", "BOOKED"),
            "cancelled_version": (CANCELLED_EVENT, None, "aggregate_version", 1),
            "cancelled_without_instant": (CANCELLED_EVENT, "payload", "cancelled_at", None),
        }
        for reason, (event_type, section, name, value) in cases.items():
            source = self.booked if event_type == BOOKED_EVENT else self.cancelled
            body = json.loads(json.dumps(source))
            target = body[section] if section else body
            target[name] = value
            with self.subTest(reason=reason):
                self.assertFalse(validator(event_type).is_valid(body))


if __name__ == "__main__":
    unittest.main()
