"""Los eventos que produce el código cumplen su esquema versionado."""

import json
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any, Self
from uuid import UUID, uuid4

from jsonschema import Draft202012Validator, FormatChecker

from sportapp_platform.testing.contracts import repository_root
from users_management.api.profile_payload import parse_demographic, parse_physiological
from users_management.application.commands.profile_section import ProfileSectionCommand
from users_management.application.commands.update_demographic_profile import (
    UpdateDemographicProfile,
)
from users_management.application.commands.update_physiological_profile import (
    UpdatePhysiologicalProfile,
)
from users_management.application.ports import (
    IdempotencyRecord,
    OutboxMessage,
    StoredProfileSection,
)
from users_management.domain.account.events import UserRegistered
from users_management.domain.account.model import ActorType, UserAccount
from users_management.domain.profile.events import ProfileSection
from users_management.infrastructure.persistence_write.outbox import ClaimedEvent
from users_management.workers.outbox_publisher import build_envelope

SCHEMA = Path("contracts/events/usuarios/usuario.registrado.v1.schema.json")


class UserRegisteredContractTest(unittest.TestCase):
    def setUp(self) -> None:
        path = repository_root(Path(__file__).resolve()) / SCHEMA
        self.validator = Draft202012Validator(
            json.loads(path.read_text(encoding="utf-8")), format_checker=FormatChecker()
        )

    def _envelope(self, correlation: bool = True) -> dict[str, object]:
        account = UserAccount.register(
            user_id=uuid4(),
            full_name="Ana Pérez",
            email="ana@example.com",
            actor_type=ActorType.SERVICE_PROVIDER,
            password_hash="$argon2id$no-se-publica",
            accepted_policies_version="2026-10",
            now=datetime(2026, 10, 7, 12, 0, tzinfo=UTC),
        )
        event = UserRegistered.from_account(account)
        return build_envelope(
            ClaimedEvent(
                event_id=uuid4(),
                event_type=UserRegistered.EVENT_TYPE,
                schema_version=UserRegistered.SCHEMA_VERSION,
                aggregate_type="user_account",
                aggregate_id=account.user_id,
                aggregate_version=account.version,
                occurred_at=account.created_at,
                correlation_id=uuid4() if correlation else None,
                causation_id=str(uuid4()),
                payload=event.payload(),
                attempts=0,
            )
        )

    def test_produced_envelope_matches_schema(self) -> None:
        self.validator.validate(self._envelope())

    def test_envelope_without_correlation_is_still_valid(self) -> None:
        self.validator.validate(self._envelope(correlation=False))

    def test_schema_rejects_credentials_in_payload(self) -> None:
        envelope = self._envelope()
        envelope["payload"] = {**envelope["payload"], "password_hash": "x"}  # type: ignore[dict-item]

        self.assertFalse(self.validator.is_valid(envelope))


NOW = datetime(2026, 10, 12, 13, 5, 0, 123456, tzinfo=UTC)
CONSENT = "health-2026-10-12.v1"


class _Clock:
    def now(self) -> datetime:
        return NOW


class _CapturingUnitOfWork:
    """Sección vigente opcional y captura de la outbox; sin persistencia."""

    def __init__(self, current: StoredProfileSection | None, outbox: list[OutboxMessage]) -> None:
        self._current = current
        self._outbox = outbox

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None

    def find_idempotency(self, key: UUID) -> IdempotencyRecord | None:
        return None

    def delete_idempotency(self, key: UUID) -> None:
        return None

    def lock_section(self, section: ProfileSection, user_id: UUID) -> StoredProfileSection | None:
        return self._current

    def save_section(self, stored: StoredProfileSection, *, created: bool) -> None:
        return None

    def add_outbox(self, message: OutboxMessage) -> None:
        self._outbox.append(message)

    def add_idempotency(self, record: IdempotencyRecord) -> None:
        return None

    def commit(self) -> None:
        return None


def _claimed(message: OutboxMessage) -> ClaimedEvent:
    return ClaimedEvent(
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


def _demographic(unit_of_work: Any) -> UpdateDemographicProfile:
    return UpdateDemographicProfile(unit_of_work=unit_of_work, clock=_Clock())


def _physiological(unit_of_work: Any) -> UpdatePhysiologicalProfile:
    return UpdatePhysiologicalProfile(
        unit_of_work=unit_of_work, clock=_Clock(), current_consent_version=CONSENT
    )


def _produce(
    build: Any, body: dict[str, Any], current: StoredProfileSection | None = None
) -> dict[str, Any]:
    """Envelope que publicaría el publicador para el evento del comando real."""
    outbox: list[OutboxMessage] = []
    use_case = build(lambda: _CapturingUnitOfWork(current, outbox))
    parse = parse_demographic if build is _demographic else parse_physiological
    use_case(uuid4(), ProfileSectionCommand(parse(body), uuid4(), uuid4()))
    [message] = outbox
    return build_envelope(_claimed(message))


DEMOGRAPHIC_BODY = {
    "birth_date": "1992-04-18",
    "gender": "FEMALE",
    "country_code": "CO",
    "city": "Bogotá",
    "marital_status": "SINGLE",
    "occupation": "Ingeniera",
    "income_range": "2_4_SMMLV",
}
PHYSIOLOGICAL_BODY = {
    "consent": {"version": CONSENT},
    "measured_at": "2026-10-12T13:00:00Z",
    "weight_kg": "68.5",
    "height_cm": 172,
    "resting_heart_rate_bpm": 58,
    "blood_pressure": {"systolic_mmhg": 118, "diastolic_mmhg": 76},
    "body_temperature_c": "36.6",
}


class ProfileEventsContractTest(unittest.TestCase):
    """CA4 y CA7: `perfil.*.v1` con el estado completo de la sección (HU005, ADR-034)."""

    def _validator(self, event_type: str) -> Draft202012Validator:
        path = repository_root(Path(__file__).resolve()) / (
            f"contracts/events/usuarios/{event_type}.schema.json"
        )
        return Draft202012Validator(
            json.loads(path.read_text(encoding="utf-8")), format_checker=FormatChecker()
        )

    def test_demographic_event_matches_schema(self) -> None:
        validator = self._validator("perfil.demografico_actualizado.v1")
        minimal = {k: DEMOGRAPHIC_BODY[k] for k in ("birth_date", "gender", "country_code", "city")}
        for body in (DEMOGRAPHIC_BODY, minimal):
            with self.subTest(fields=sorted(body)):
                envelope = _produce(_demographic, body)
                validator.validate(envelope)
                self.assertEqual(envelope["payload"]["version"], envelope["aggregate_version"])
                self.assertEqual(envelope["payload"]["user_id"], envelope["aggregate_id"])

    def test_physiological_event_matches_schema(self) -> None:
        validator = self._validator("perfil.fisiologico_actualizado.v1")
        minimal = {
            k: PHYSIOLOGICAL_BODY[k] for k in ("consent", "measured_at", "weight_kg", "height_cm")
        }
        for body in (PHYSIOLOGICAL_BODY, minimal):
            with self.subTest(fields=sorted(body)):
                envelope = _produce(_physiological, body)
                validator.validate(envelope)
                self.assertEqual(envelope["payload"]["bmi"], "23.2")

    def test_replacement_event_matches_schema(self) -> None:
        validator = self._validator("perfil.demografico_actualizado.v1")
        current = StoredProfileSection(
            section=ProfileSection.DEMOGRAPHIC,
            user_id=uuid4(),
            version=4,
            document={"version": 4, "created_at": "2026-10-01T08:00:00Z"},
        )

        envelope = _produce(_demographic, {**DEMOGRAPHIC_BODY, "expected_version": 4}, current)

        validator.validate(envelope)
        self.assertEqual(envelope["aggregate_version"], 5)
        self.assertEqual(envelope["payload"]["created_at"], "2026-10-01T08:00:00Z")
        self.assertEqual(envelope["payload"]["updated_at"], "2026-10-12T13:05:00.123456Z")

    def test_schemas_reject_excluded_or_malformed_data(self) -> None:
        demographic = self._validator("perfil.demografico_actualizado.v1")
        envelope = _produce(_demographic, DEMOGRAPHIC_BODY)
        for extra in ({"ethnicity": "x"}, {"religion": "x"}):
            with self.subTest(extra=extra):
                tampered = {**envelope, "payload": {**envelope["payload"], **extra}}
                self.assertFalse(demographic.is_valid(tampered))
        physiological = self._validator("perfil.fisiologico_actualizado.v1")
        envelope = _produce(_physiological, PHYSIOLOGICAL_BODY)
        without_bmi = {key: value for key, value in envelope["payload"].items() if key != "bmi"}
        self.assertFalse(physiological.is_valid({**envelope, "payload": without_bmi}))
        as_number = {**envelope["payload"], "weight_kg": 68.5}
        self.assertFalse(physiological.is_valid({**envelope, "payload": as_number}))


if __name__ == "__main__":
    unittest.main()
