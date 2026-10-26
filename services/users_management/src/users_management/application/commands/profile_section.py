"""HU005: registrar o reemplazar una sección del perfil del deportista (ADR-034).

Secuencia común a las secciones:
1. Validar y normalizar todo el cuerpo sin tocar persistencia (CA2, CA3).
2. Si la clave de idempotencia ya tiene un resultado vigente, devolver el mismo código
   y cuerpo; con otro payload u otro deportista, `idempotency_key_reused` (CA6).
3. En una sola transacción: bloquear la sección, volver a comprobar la clave, verificar
   `expected_version` (CA6), guardar la sección, `outbox_event` y la clave (CA1, CA4).
4. Responder después del `COMMIT`, sin esperar la proyección.

La auditoría registra sección, versión y resultado; nunca valores (CA4).

HU008 reutiliza la secuencia para editar parcialmente una sección existente: con
`not_found_code` la sección debe existir (si no, `SectionNotFound`) y `section_fields`
fusiona lo enviado con el documento vigente, ya bloqueado.
"""

import hashlib
import json
import logging
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from http import HTTPStatus
from typing import Any
from uuid import UUID, uuid4

from users_management.application.errors import (
    IdempotencyKeyConflict,
    IdempotencyKeyReused,
    SectionAlreadyExists,
)
from users_management.application.ports import (
    Clock,
    IdempotencyRecord,
    OutboxMessage,
    ProfileUnitOfWork,
    StoredProfileSection,
)
from users_management.domain.profile.events import (
    AGGREGATE_TYPE_BY_SECTION,
    EVENT_TYPE_BY_SECTION,
    SCHEMA_VERSION,
    ProfileSection,
)
from users_management.domain.profile.section import (
    NO_SECTION_VERSION,
    SectionNotFound,
    ValidSection,
    VersionConflict,
    next_version,
    section_document,
)
from users_management.domain.profile.validation import InvalidProfileSection, format_instant

IDEMPOTENCY_TTL = timedelta(hours=24)

_audit = logging.getLogger("sportapp.audit")


@dataclass(frozen=True)
class ProfileSectionCommand[InputT]:
    section_input: InputT
    idempotency_key: UUID
    correlation_id: UUID | None = None

    def __repr__(self) -> str:
        # El contenido de la sección es sensible: nunca en trazas.
        return f"ProfileSectionCommand(idempotency_key={self.idempotency_key})"


@dataclass(frozen=True)
class ProfileSectionResult:
    """`status` es el código original (201 al crear, 200 al reemplazar); un reintento lo
    repite. `document` es el cuerpo de la respuesta y el payload del evento."""

    status: int
    document: dict[str, Any]
    replayed: bool = False

    def __repr__(self) -> str:
        return f"ProfileSectionResult(status={self.status}, replayed={self.replayed})"

    @property
    def version(self) -> int:
        return int(self.document["version"])


def request_fingerprint(operation: str, user_id: UUID, normalized: dict[str, Any]) -> str:
    """Huella del payload normalizado y del deportista autenticado."""
    canonical = json.dumps(
        [operation, str(user_id), normalized], ensure_ascii=False, sort_keys=True
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class UpdateProfileSection[InputT](ABC):
    section: ProfileSection
    operation: str
    # Código del `404` cuando el comando solo edita una sección existente (HU008).
    not_found_code: str | None = None
    # Resultado de auditoría de un `200` (reemplazo completo o edición parcial).
    updated_outcome = "replaced"

    def __init__(
        self,
        *,
        unit_of_work: Callable[[], ProfileUnitOfWork],
        clock: Clock,
        new_id: Callable[[], UUID] = uuid4,
    ) -> None:
        self._unit_of_work = unit_of_work
        self._clock = clock
        self._new_id = new_id

    @abstractmethod
    def validate(self, section_input: InputT) -> ValidSection:
        """Reglas de la sección sin persistir; `InvalidProfileSection` con todas las
        violaciones. También sirve para informarlas junto con una clave inválida."""

    def section_fields(
        self, valid: ValidSection, current: StoredProfileSection | None, now: datetime
    ) -> dict[str, Any]:
        """Campos del documento nuevo; por defecto la sección enviada reemplaza a la vigente."""
        return valid.fields(now)

    def __call__(
        self, user_id: UUID, command: ProfileSectionCommand[InputT]
    ) -> ProfileSectionResult:
        try:
            result = self._execute(user_id, command)
        except (
            InvalidProfileSection,
            IdempotencyKeyReused,
            VersionConflict,
            SectionNotFound,
        ) as error:
            self._audit(user_id, None, "rejected", command.correlation_id, reason=error.code)
            raise
        if result.replayed:
            outcome = "replayed"
        elif result.status == HTTPStatus.CREATED:
            outcome = "created"
        else:
            outcome = self.updated_outcome
        self._audit(user_id, result.version, outcome, command.correlation_id)
        return result

    def _audit(
        self,
        user_id: UUID,
        version: int | None,
        result: str,
        correlation_id: UUID | None,
        reason: str | None = None,
    ) -> None:
        fields: dict[str, Any] = {
            "user_id": str(user_id),
            "section": self.section.value,
            # `section_version` y no `version`: el formateador JSON ya usa `version` para la
            # versión del servicio y un campo con ese nombre la sobrescribiría.
            "section_version": version,
            "result": result,
            "correlation_id": str(correlation_id) if correlation_id else None,
        }
        if reason is not None:
            fields["reason"] = reason
        _audit.info("audit.profile_section_updated", extra={"fields": fields})

    def _execute(
        self, user_id: UUID, command: ProfileSectionCommand[InputT]
    ) -> ProfileSectionResult:
        valid = self.validate(command.section_input)
        fingerprint = request_fingerprint(self.operation, user_id, valid.normalized())
        previous = self._previous(user_id, command.idempotency_key, fingerprint)
        if previous is not None:
            return previous
        try:
            return self._write(user_id, command, valid, fingerprint)
        except (IdempotencyKeyConflict, SectionAlreadyExists) as error:
            # Una solicitud concurrente confirmó primero: con la misma clave se devuelve su
            # resultado; si fue otra alta de la sección, es un conflicto de versión.
            previous = self._previous(user_id, command.idempotency_key, fingerprint)
            if previous is not None:
                return previous
            if isinstance(error, SectionAlreadyExists):
                raise VersionConflict(self._current_version(user_id)) from None
            raise

    def _previous(self, user_id: UUID, key: UUID, fingerprint: str) -> ProfileSectionResult | None:
        with self._unit_of_work() as unit_of_work:
            record = unit_of_work.find_idempotency(key)
        if record is None or record.expires_at <= self._clock.now():
            return None
        if (
            record.operation != self.operation
            or record.resource_id != user_id
            or record.request_fingerprint != fingerprint
        ):
            raise IdempotencyKeyReused()
        return ProfileSectionResult(
            status=int(record.response["status"]),
            document=record.response["body"],
            replayed=True,
        )

    def _current_version(self, user_id: UUID) -> int:
        with self._unit_of_work() as unit_of_work:
            current = unit_of_work.lock_section(self.section, user_id)
        return current.version if current is not None else NO_SECTION_VERSION

    def _write(
        self,
        user_id: UUID,
        command: ProfileSectionCommand[InputT],
        valid: ValidSection,
        fingerprint: str,
    ) -> ProfileSectionResult:
        key = command.idempotency_key
        with self._unit_of_work() as unit_of_work:
            current = unit_of_work.lock_section(self.section, user_id)
            now = self._clock.now()
            # La clave se vuelve a consultar con la sección bloqueada: un reintento
            # concurrente que esperó el bloqueo encuentra el resultado del primero.
            existing = unit_of_work.find_idempotency(key)
            if existing is not None:
                if existing.expires_at > now:
                    raise IdempotencyKeyConflict()
                unit_of_work.delete_idempotency(key)
            if current is None and self.not_found_code is not None:
                raise SectionNotFound(self.not_found_code)
            version = next_version(
                valid.expected_version, current.version if current is not None else None
            )
            created = current is None
            document = section_document(
                user_id=user_id,
                version=version,
                fields=self.section_fields(valid, current, now),
                created_at=(
                    format_instant(now) if current is None else current.document["created_at"]
                ),
                updated_at=now,
            )
            unit_of_work.save_section(
                StoredProfileSection(
                    section=self.section, user_id=user_id, version=version, document=document
                ),
                created=created,
            )
            unit_of_work.add_outbox(
                OutboxMessage(
                    event_id=self._new_id(),
                    event_type=EVENT_TYPE_BY_SECTION[self.section],
                    schema_version=SCHEMA_VERSION,
                    aggregate_type=AGGREGATE_TYPE_BY_SECTION[self.section],
                    aggregate_id=user_id,
                    aggregate_version=version,
                    occurred_at=now,
                    correlation_id=command.correlation_id,
                    causation_id=str(key),
                    payload=document,
                )
            )
            status = HTTPStatus.CREATED if created else HTTPStatus.OK
            unit_of_work.add_idempotency(
                IdempotencyRecord(
                    key=key,
                    operation=self.operation,
                    request_fingerprint=fingerprint,
                    resource_id=user_id,
                    response={"status": status.value, "body": document},
                    created_at=now,
                    expires_at=now + IDEMPOTENCY_TTL,
                )
            )
            unit_of_work.commit()
        return ProfileSectionResult(status=status.value, document=document)
