"""El evento que produce el código cumple su esquema versionado."""

import json
import unittest
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from jsonschema import Draft202012Validator, FormatChecker

from sportapp_platform.testing.contracts import repository_root
from users_management.domain.account.events import UserRegistered
from users_management.domain.account.model import ActorType, UserAccount
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


if __name__ == "__main__":
    unittest.main()
