"""HU002: editar nombre y correo de la cuenta propia (ADR-035).

Secuencia:
1. Validar y normalizar todo el cuerpo sin tocar persistencia (CA2).
2. Si la clave de idempotencia ya tiene un resultado vigente, devolver el mismo cuerpo; con
   otro payload u otra cuenta, `idempotency_key_reused` (CA5).
3. En una sola transacción: bloquear `user_account`, volver a comprobar la clave, verificar
   `expected_version` (CA4) y la unicidad del correo (CA3), guardar la cuenta con una versión
   nueva, `usuario.actualizado.v1` en la outbox y la clave (CA1, CA5). Si nada cambia (p. ej.,
   el mismo correo) no hay versión ni evento.
4. Responder después del `COMMIT`, sin esperar la proyección.

El cambio de correo no revoca sesiones (ADR-035 §4): los refresh tokens y el `sub` del
access token no dependen del correo. La auditoría lleva los nombres de los campos
cambiados, nunca sus valores (CA5).
"""

import hashlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

from users_management.application.errors import (
    AccountNotFound,
    IdempotencyKeyConflict,
    IdempotencyKeyReused,
)
from users_management.application.ports import (
    AccountUnitOfWork,
    Clock,
    IdempotencyRecord,
    OutboxMessage,
)
from users_management.domain.account.editing import (
    AccountChange,
    AccountChangeInput,
    validate_account_change,
)
from users_management.domain.account.errors import (
    AccountVersionConflict,
    EmailAlreadyRegistered,
)
from users_management.domain.account.events import AGGREGATE_TYPE, UserUpdated, account_document
from users_management.domain.profile.validation import InvalidProfileSection

OPERATION = "update_account"
IDEMPOTENCY_TTL = timedelta(hours=24)

_audit = logging.getLogger("sportapp.audit")


@dataclass(frozen=True)
class UpdateAccountCommand:
    change: AccountChangeInput
    idempotency_key: UUID
    correlation_id: UUID | None = None

    def __repr__(self) -> str:
        return f"UpdateAccountCommand(idempotency_key={self.idempotency_key})"


@dataclass(frozen=True)
class AccountResult:
    """`document` es el cuerpo de la respuesta `200` (y el payload del evento si hubo
    cambio). `changed_fields` solo se conoce en la ejecución original."""

    document: dict[str, Any]
    changed_fields: tuple[str, ...] = ()
    replayed: bool = False

    def __repr__(self) -> str:
        return f"AccountResult(version={self.version}, replayed={self.replayed})"

    @property
    def version(self) -> int:
        return int(self.document["version"])


def request_fingerprint(user_id: UUID, normalized: dict[str, Any]) -> str:
    canonical = json.dumps(
        [OPERATION, str(user_id), normalized], ensure_ascii=False, sort_keys=True
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class UpdateAccount:
    def __init__(
        self,
        *,
        unit_of_work: Callable[[], AccountUnitOfWork],
        clock: Clock,
        new_id: Callable[[], UUID] = uuid4,
    ) -> None:
        self._unit_of_work = unit_of_work
        self._clock = clock
        self._new_id = new_id

    @staticmethod
    def validate(change: AccountChangeInput) -> AccountChange:
        return validate_account_change(change)

    def __call__(self, user_id: UUID, command: UpdateAccountCommand) -> AccountResult:
        try:
            result = self._execute(user_id, command)
        except (
            InvalidProfileSection,
            IdempotencyKeyReused,
            AccountVersionConflict,
            EmailAlreadyRegistered,
        ) as error:
            self._audit(
                user_id=user_id,
                version=None,
                changed_fields=(),
                result="rejected",
                correlation_id=command.correlation_id,
                reason=error.code,
            )
            raise
        if result.replayed:
            outcome = "replayed"
        else:
            outcome = "updated" if result.changed_fields else "unchanged"
        self._audit(
            user_id=user_id,
            version=result.version,
            changed_fields=result.changed_fields,
            result=outcome,
            correlation_id=command.correlation_id,
        )
        return result

    @staticmethod
    def _audit(
        *,
        user_id: UUID,
        version: int | None,
        changed_fields: tuple[str, ...],
        result: str,
        correlation_id: UUID | None,
        reason: str | None = None,
    ) -> None:
        fields: dict[str, Any] = {
            "user_id": str(user_id),
            # `account_version` y no `version`: el formateador JSON usa `version` para la
            # versión del servicio y la sobrescribiría.
            "account_version": version,
            "changed_fields": list(changed_fields),
            "result": result,
            "correlation_id": str(correlation_id) if correlation_id else None,
        }
        if reason is not None:
            fields["reason"] = reason
        _audit.info("audit.account_updated", extra={"fields": fields})

    def _execute(self, user_id: UUID, command: UpdateAccountCommand) -> AccountResult:
        change = self.validate(command.change)
        fingerprint = request_fingerprint(user_id, change.normalized())
        previous = self._previous(user_id, command.idempotency_key, fingerprint)
        if previous is not None:
            return previous
        try:
            return self._write(user_id, command, change, fingerprint)
        except (IdempotencyKeyConflict, EmailAlreadyRegistered):
            # Una solicitud concurrente con la misma clave confirmó primero: se devuelve su
            # resultado. Si no, el correo lo tomó otra cuenta en paralelo (CA3).
            previous = self._previous(user_id, command.idempotency_key, fingerprint)
            if previous is not None:
                return previous
            raise

    def _previous(self, user_id: UUID, key: UUID, fingerprint: str) -> AccountResult | None:
        with self._unit_of_work() as unit_of_work:
            record = unit_of_work.find_idempotency(key)
        if record is None or record.expires_at <= self._clock.now():
            return None
        if (
            record.operation != OPERATION
            or record.resource_id != user_id
            or record.request_fingerprint != fingerprint
        ):
            raise IdempotencyKeyReused()
        return AccountResult(document=record.response["body"], replayed=True)

    def _write(
        self,
        user_id: UUID,
        command: UpdateAccountCommand,
        change: AccountChange,
        fingerprint: str,
    ) -> AccountResult:
        key = command.idempotency_key
        with self._unit_of_work() as unit_of_work:
            current = unit_of_work.lock_account(user_id)
            if current is None:
                raise AccountNotFound()
            now = self._clock.now()
            # La clave se vuelve a consultar con la cuenta bloqueada: un reintento concurrente
            # que esperó el bloqueo encuentra el resultado del primero.
            existing = unit_of_work.find_idempotency(key)
            if existing is not None:
                if existing.expires_at > now:
                    raise IdempotencyKeyConflict()
                unit_of_work.delete_idempotency(key)
            account, changed = current.change(
                expected_version=change.expected_version,
                full_name=change.full_name,
                email=change.email,
                now=now,
            )
            document = account_document(account)
            if changed:
                if "email" in changed and unit_of_work.email_taken(account.email, user_id):
                    raise EmailAlreadyRegistered()
                unit_of_work.save_account(account)
                unit_of_work.add_outbox(
                    OutboxMessage(
                        event_id=self._new_id(),
                        event_type=UserUpdated.EVENT_TYPE,
                        schema_version=UserUpdated.SCHEMA_VERSION,
                        aggregate_type=AGGREGATE_TYPE,
                        aggregate_id=user_id,
                        aggregate_version=account.version,
                        occurred_at=now,
                        correlation_id=command.correlation_id,
                        causation_id=str(key),
                        payload=document,
                    )
                )
            unit_of_work.add_idempotency(
                IdempotencyRecord(
                    key=key,
                    operation=OPERATION,
                    request_fingerprint=fingerprint,
                    resource_id=user_id,
                    response={"status": 200, "body": document},
                    created_at=now,
                    expires_at=now + IDEMPOTENCY_TTL,
                )
            )
            unit_of_work.commit()
        return AccountResult(document=document, changed_fields=changed)
