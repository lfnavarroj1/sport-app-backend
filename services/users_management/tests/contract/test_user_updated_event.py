"""HU002 (ADR-035): `usuario.actualizado.v1` producido por `UpdateAccount` cumple su esquema
versionado y lo acepta `usuarios-read-projector`; el OpenAPI publica la cuenta."""

import json
import unittest
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from jsonschema import Draft202012Validator, FormatChecker

from sportapp_platform.testing.contracts import openapi_contract_path, repository_root
from users_management.application.commands.update_account import (
    UpdateAccount,
    UpdateAccountCommand,
)
from users_management.application.messages import UserUpdatedEvent, parse_users_event
from users_management.application.ports import IdempotencyRecord, OutboxMessage
from users_management.domain.account.editing import AccountChangeInput
from users_management.domain.account.model import AccountDetails, AccountStatus, ActorType
from users_management.infrastructure.persistence_write.outbox import ClaimedEvent
from users_management.workers.outbox_publisher import build_envelope

ROOT = repository_root(Path(__file__).resolve())
SCHEMA = ROOT / "contracts/events/usuarios/usuario.actualizado.v1.schema.json"
NOW = datetime(2026, 10, 24, 12, 0, 30, 123456, tzinfo=UTC)


class _Clock:
    def now(self) -> datetime:
        return NOW


class _UnitOfWork:
    """Una cuenta y lo que el comando deja para la outbox."""

    def __init__(self, account: AccountDetails, outbox: list[OutboxMessage]) -> None:
        self._account = account
        self._outbox = outbox

    def __enter__(self) -> "_UnitOfWork":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def find_idempotency(self, key: object) -> IdempotencyRecord | None:
        return None

    def delete_idempotency(self, key: object) -> None:
        return None

    def lock_account(self, user_id: object) -> AccountDetails:
        return self._account

    def email_taken(self, email: str, user_id: object) -> bool:
        return False

    def save_account(self, account: AccountDetails) -> None:
        return None

    def add_outbox(self, message: OutboxMessage) -> None:
        self._outbox.append(message)

    def add_idempotency(self, record: IdempotencyRecord) -> None:
        return None

    def commit(self) -> None:
        return None


def produced_envelope(actor_type: ActorType, status: AccountStatus) -> dict[str, Any]:
    account = AccountDetails(
        user_id=uuid4(),
        full_name="Ana Pérez",
        email="ana@example.com",
        actor_type=actor_type,
        status=status,
        version=4,
        updated_at=datetime(2026, 10, 1, tzinfo=UTC),
    )
    outbox: list[OutboxMessage] = []
    UpdateAccount(unit_of_work=lambda: _UnitOfWork(account, outbox), clock=_Clock())(  # type: ignore[arg-type,return-value]
        account.user_id,
        UpdateAccountCommand(
            change=AccountChangeInput(
                expected_version=4, full_name="Ana María Pérez", email="ana.maria@example.com"
            ),
            idempotency_key=uuid4(),
            correlation_id=uuid4(),
        ),
    )
    [message] = outbox
    return build_envelope(
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


class UserUpdatedContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        self.validator = Draft202012Validator(self.schema, format_checker=FormatChecker())

    def test_produced_envelope_matches_schema_for_every_actor_and_status(self) -> None:
        for actor_type in ActorType:
            for status in AccountStatus:
                with self.subTest(actor_type=actor_type, status=status):
                    envelope = produced_envelope(actor_type, status)

                    self.validator.validate(envelope)
                    self.assertEqual(envelope["aggregate_version"], 5)
                    self.assertEqual(envelope["payload"]["version"], 5)
                    self.assertEqual(
                        envelope["payload"]["updated_at"], "2026-10-24T12:00:30.123456Z"
                    )

    def test_published_event_is_accepted_by_the_projector(self) -> None:
        envelope = produced_envelope(ActorType.SERVICE_PROVIDER, AccountStatus.ACTIVE)

        event = parse_users_event(envelope)

        self.assertIsInstance(event, UserUpdatedEvent)
        self.assertEqual(event.aggregate_version, 5)

    def test_schema_rejects_credentials_and_policies(self) -> None:
        envelope = produced_envelope(ActorType.ATHLETE, AccountStatus.ACTIVE)
        for extra in ("password_hash", "accepted_policies_version", "password"):
            with self.subTest(extra=extra):
                tampered = {**envelope, "payload": {**envelope["payload"], extra: "x"}}

                self.assertFalse(self.validator.is_valid(tampered))

    def test_schema_rejects_version_one_and_local_offsets(self) -> None:
        envelope = produced_envelope(ActorType.ATHLETE, AccountStatus.ACTIVE)

        self.assertFalse(self.validator.is_valid({**envelope, "aggregate_version": 1}))
        self.assertFalse(
            self.validator.is_valid(
                {
                    **envelope,
                    "payload": {**envelope["payload"], "updated_at": "2026-10-24T07:00:00-05:00"},
                }
            )
        )

    def test_catalogs_match_the_domain(self) -> None:
        payload = self.schema["properties"]["payload"]["properties"]

        self.assertEqual(set(payload["actor_type"]["enum"]), {item.value for item in ActorType})
        self.assertEqual(set(payload["status"]["enum"]), {item.value for item in AccountStatus})


class AccountOpenApiContractTest(unittest.TestCase):
    """Las dos operaciones de ADR-035 §2 y §6 con sus errores estables."""

    def setUp(self) -> None:
        document = json.loads(
            openapi_contract_path(ROOT, "users-management").read_text(encoding="utf-8")
        )
        self.operations = document["paths"]["/v1/me/account"]
        self.schemas = document["components"]["schemas"]

    def test_put_contract(self) -> None:
        put = self.operations["put"]
        body = put["requestBody"]["content"]["application/json"]["schema"]

        self.assertEqual(set(body["required"]), {"expected_version", "full_name", "email"})
        self.assertFalse(body["additionalProperties"])
        self.assertEqual({"200", "401", "409", "422"}, set(put["responses"]) - {"default"})
        for code in ("version_conflict", "email_already_registered", "idempotency_key_reused"):
            self.assertIn(code, put["responses"]["409"]["description"])
        self.assertEqual(
            set(self.schemas["AccountResponse"]["properties"]),
            {"user_id", "full_name", "email", "actor_type", "status", "version", "updated_at"},
        )

    def test_get_contract(self) -> None:
        get = self.operations["get"]

        self.assertLessEqual({"200", "401", "404", "503"}, set(get["responses"]))
        self.assertEqual(
            set(self.schemas["AccountReadResponse"]["properties"]),
            {"user_id", "full_name", "email", "actor_type", "status", "version", "projected_at"},
        )
