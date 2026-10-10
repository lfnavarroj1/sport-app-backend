"""HU001: registrar usuario.

Secuencia:
1. Validar y normalizar (sin tocar persistencia ni calcular el hash).
2. Si la clave de idempotencia ya tiene un resultado vigente, devolverlo (CA5).
3. Calcular el hash Argon2id fuera de la transacción.
4. En una sola transacción: verificar unicidad del correo, crear la cuenta,
   registrar `usuario.registrado.v1` en la outbox y guardar el resultado
   idempotente (CA1, CA2, CA4).
"""

import hashlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from users_management.application.errors import IdempotencyKeyConflict, IdempotencyKeyReused
from users_management.application.ports import (
    Clock,
    IdempotencyRecord,
    OutboxMessage,
    PasswordHasher,
    RegistrationUnitOfWork,
)
from users_management.domain.account.errors import AccountError, EmailAlreadyRegistered
from users_management.domain.account.events import AGGREGATE_TYPE, UserRegistered
from users_management.domain.account.model import (
    ActorType,
    UserAccount,
    normalize_email,
    normalize_full_name,
    validate_password,
    validate_policies_acceptance,
)

OPERATION = "register_user"
IDEMPOTENCY_TTL = timedelta(hours=24)

_audit = logging.getLogger("sportapp.audit")


@dataclass(frozen=True)
class RegisterUserCommand:
    full_name: str
    email: str
    password: str
    actor_type: ActorType
    accepted_policies_version: str
    idempotency_key: UUID
    correlation_id: UUID | None = None

    def __repr__(self) -> str:
        # Nunca exponer la contraseña en trazas o logs.
        return f"RegisterUserCommand(idempotency_key={self.idempotency_key})"


@dataclass(frozen=True)
class RegistrationResult:
    user_id: UUID
    full_name: str
    email: str
    actor_type: ActorType
    status: str
    aggregate_version: int
    created_at: datetime
    replayed: bool = False

    @classmethod
    def from_account(cls, account: UserAccount) -> "RegistrationResult":
        return cls(
            user_id=account.user_id,
            full_name=account.full_name,
            email=account.email,
            actor_type=account.actor_type,
            status=account.status.value,
            aggregate_version=account.version,
            created_at=account.created_at,
        )

    def to_document(self) -> dict[str, Any]:
        return {
            "user_id": str(self.user_id),
            "full_name": self.full_name,
            "email": self.email,
            "actor_type": self.actor_type.value,
            "status": self.status,
            "aggregate_version": self.aggregate_version,
            "created_at": self.created_at.isoformat(),
        }

    @classmethod
    def from_document(cls, document: dict[str, Any], *, replayed: bool) -> "RegistrationResult":
        return cls(
            user_id=UUID(document["user_id"]),
            full_name=document["full_name"],
            email=document["email"],
            actor_type=ActorType(document["actor_type"]),
            status=document["status"],
            aggregate_version=int(document["aggregate_version"]),
            created_at=datetime.fromisoformat(document["created_at"]),
            replayed=replayed,
        )


def request_fingerprint(
    full_name: str, email: str, actor_type: ActorType, policies_version: str
) -> str:
    """Identifica la solicitud para detectar reutilización de la clave.

    Excluye la contraseña a propósito: un hash rápido de la contraseña sería
    atacable por fuerza bruta si se filtrara esta tabla.
    """
    canonical = json.dumps(
        [OPERATION, full_name, email, actor_type.value, policies_version], ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class RegisterUser:
    def __init__(
        self,
        *,
        unit_of_work: Callable[[], RegistrationUnitOfWork],
        password_hasher: PasswordHasher,
        clock: Clock,
        current_policies_version: str,
        new_id: Callable[[], UUID] = uuid4,
    ) -> None:
        self._unit_of_work = unit_of_work
        self._password_hasher = password_hasher
        self._clock = clock
        self._current_policies_version = current_policies_version
        self._new_id = new_id

    def __call__(self, command: RegisterUserCommand) -> RegistrationResult:
        try:
            result = self._execute(command)
        except (AccountError, IdempotencyKeyReused) as error:
            _audit.info(
                "audit.user_registration",
                extra={"fields": {"result": "rejected", "reason": error.code}},
            )
            raise
        _audit.info(
            "audit.user_registration",
            extra={
                "fields": {
                    "result": "replayed" if result.replayed else "created",
                    "user_id": str(result.user_id),
                    "actor_type": result.actor_type.value,
                }
            },
        )
        return result

    def _execute(self, command: RegisterUserCommand) -> RegistrationResult:
        full_name = normalize_full_name(command.full_name)
        email = normalize_email(command.email)
        validate_password(command.password)
        validate_policies_acceptance(
            command.accepted_policies_version, self._current_policies_version
        )
        fingerprint = request_fingerprint(
            full_name, email, command.actor_type, command.accepted_policies_version
        )

        previous = self._previous_result(command.idempotency_key, fingerprint)
        if previous is not None:
            return previous

        password_hash = self._password_hasher.hash(command.password)
        try:
            return self._register(command, full_name, email, password_hash, fingerprint)
        except (IdempotencyKeyConflict, EmailAlreadyRegistered):
            # Si una solicitud concurrente con la misma clave confirmó primero (y por eso
            # el correo ya existe), se devuelve su resultado en lugar de un conflicto.
            previous = self._previous_result(command.idempotency_key, fingerprint)
            if previous is None:
                raise
            return previous

    def _previous_result(self, key: UUID, fingerprint: str) -> RegistrationResult | None:
        with self._unit_of_work() as unit_of_work:
            record = unit_of_work.find_idempotency(key)
        if record is None or record.expires_at <= self._clock.now():
            return None
        if record.operation != OPERATION or record.request_fingerprint != fingerprint:
            raise IdempotencyKeyReused()
        return RegistrationResult.from_document(record.response, replayed=True)

    def _register(
        self,
        command: RegisterUserCommand,
        full_name: str,
        email: str,
        password_hash: str,
        fingerprint: str,
    ) -> RegistrationResult:
        now = self._clock.now()
        account = UserAccount.register(
            user_id=self._new_id(),
            full_name=full_name,
            email=email,
            actor_type=command.actor_type,
            password_hash=password_hash,
            accepted_policies_version=command.accepted_policies_version,
            now=now,
        )
        event = UserRegistered.from_account(account)
        result = RegistrationResult.from_account(account)

        with self._unit_of_work() as unit_of_work:
            existing = unit_of_work.find_idempotency(command.idempotency_key)
            if existing is not None:
                if existing.expires_at > now:
                    # Confirmada por una solicitud concurrente: se devuelve su resultado.
                    raise IdempotencyKeyConflict()
                unit_of_work.delete_idempotency(command.idempotency_key)
            if unit_of_work.email_exists(email):
                raise EmailAlreadyRegistered()
            unit_of_work.add_account(account)
            unit_of_work.add_outbox(
                OutboxMessage(
                    event_id=self._new_id(),
                    event_type=UserRegistered.EVENT_TYPE,
                    schema_version=UserRegistered.SCHEMA_VERSION,
                    aggregate_type=AGGREGATE_TYPE,
                    aggregate_id=account.user_id,
                    aggregate_version=account.version,
                    occurred_at=now,
                    correlation_id=command.correlation_id,
                    causation_id=str(command.idempotency_key),
                    payload=event.payload(),
                )
            )
            unit_of_work.add_idempotency(
                IdempotencyRecord(
                    key=command.idempotency_key,
                    operation=OPERATION,
                    request_fingerprint=fingerprint,
                    resource_id=account.user_id,
                    response=result.to_document(),
                    created_at=now,
                    expires_at=now + IDEMPOTENCY_TTL,
                )
            )
            unit_of_work.commit()
        return result
