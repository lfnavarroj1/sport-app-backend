"""HU039: el sobre que publica `eventos-outbox-publisher` cumple `notificacion.solicitada.v1`
(ADR-045), el proyector acepta exactamente ese contrato, y la tabla de alias de disciplina es
copia exacta de la de `bff-mobile` (ADR-041)."""

import json
import unittest
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from events_management.api.payload import parse_event
from events_management.application.messages import (
    NotificationRequestedMessage,
    parse_sport_event_message,
)
from events_management.domain.notifications.events import (
    AGGREGATE_TYPE,
    EVENT_TYPE,
    SCHEMA_VERSION,
    requested_payload,
)
from events_management.domain.notifications.model import NotificationType, new_notification
from events_management.domain.sport_events.model import SportEvent, validate_event
from events_management.infrastructure.persistence_write.outbox import ClaimedEvent
from events_management.workers.outbox_publisher import build_envelope, ordering_key
from sportapp_platform.testing.contracts import repository_root

ROOT = repository_root(Path(__file__).resolve())
SCHEMA = ROOT / "contracts" / "events" / "eventos" / "notificacion.solicitada.v1.schema.json"
ALIASES = "discipline-aliases.v1.json"
EVENTS_ALIASES = (
    ROOT / "services/events_management/src/events_management/domain/notifications" / ALIASES
)
MOBILE_ALIASES = ROOT / "bff/mobile/src/bff_mobile/api" / ALIASES
NOW = datetime(2026, 10, 12, 22, 30, tzinfo=UTC)


def an_event() -> SportEvent:
    body = {
        "name": "Carrera 10K Bogotá",
        "description": "Carrera recreativa de diez kilómetros para corredores aficionados.",
        "discipline": "Running",
        "location": {
            "name": "Parque Simón Bolívar",
            "address": "Avenida Calle 63 # 60-50",
            "city": "Bogotá",
            "country_code": "CO",
        },
        "starts_at": "2026-11-15T07:00:00-05:00",
        "time_zone": "America/Bogota",
        "duration_minutes": 180,
        "capacity": 5000,
    }
    return SportEvent.draft(
        event_id=uuid.uuid4(),
        organizer_id=uuid.uuid4(),
        details=validate_event(parse_event(body), NOW),
        now=NOW,
    )


def published(kind: NotificationType) -> tuple[dict[str, Any], ClaimedEvent]:
    item = new_notification(
        notification_id=uuid.uuid4(),
        notification_type=kind,
        event=an_event(),
        recipient_id=uuid.uuid4(),
        now=NOW,
    )
    claimed = ClaimedEvent(
        event_id=uuid.uuid4(),
        event_type=EVENT_TYPE,
        schema_version=SCHEMA_VERSION,
        aggregate_type=AGGREGATE_TYPE,
        aggregate_id=item.notification_id,
        aggregate_version=1,
        occurred_at=NOW,
        correlation_id=uuid.uuid4(),
        causation_id=str(uuid.uuid4()),
        payload=requested_payload(item),
        attempts=0,
        ordering_key=str(item.recipient_id),
    )
    loaded: dict[str, Any] = json.loads(json.dumps(build_envelope(claimed)))
    return loaded, claimed


class NotificationEventContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.validator = Draft202012Validator(
            json.loads(SCHEMA.read_text(encoding="utf-8")), format_checker=FormatChecker()
        )

    def test_both_types_match_schema_and_are_ordered_by_recipient(self) -> None:
        for kind in NotificationType:
            body, claimed = published(kind)
            with self.subTest(kind=kind):
                self.validator.validate(body)
                self.assertEqual(body["payload"]["notification_id"], body["aggregate_id"])
                self.assertEqual(ordering_key(claimed), body["payload"]["recipient_id"])

    def test_schema_rejects_personal_data_and_wrong_priority(self) -> None:
        body, _ = published(NotificationType.EVENT_PUBLISHED_MATCH)
        for change in ({"email": "a@example.com"}, {"city": "Bogotá"}, {"priority": "HIGH"}):
            altered = {**body, "payload": {**body["payload"], **change}}
            with self.subTest(change=change):
                self.assertFalse(self.validator.is_valid(altered))

    def test_projector_parses_what_the_producer_publishes(self) -> None:
        body, _ = published(NotificationType.EVENT_REMINDER_24H)

        message = parse_sport_event_message(body)

        assert isinstance(message, NotificationRequestedMessage)
        self.assertEqual(str(message.notification.notification_id), body["aggregate_id"])
        self.assertEqual(message.notification.priority.value, "HIGH")


class DisciplineAliasesCopyTest(unittest.TestCase):
    def test_events_copy_is_identical_to_bff_mobile(self) -> None:
        """ADR-041: una sola tabla `discipline-aliases.v1`; ampliarla exige cambiar ambas."""
        self.assertEqual(
            json.loads(EVENTS_ALIASES.read_text(encoding="utf-8")),
            json.loads(MOBILE_ALIASES.read_text(encoding="utf-8")),
        )


if __name__ == "__main__":
    unittest.main()
