"""`usuarios-read-projector`: aplica los eventos de usuarios en la base de lectura (CMP-001).

Cada evento se aplica en una sola transacción de la base de lectura (secuencia de CMP-001):

1. registra el `event_id` en `processed_event` con `ON CONFLICT DO NOTHING`; si ya estaba,
   es una entrega repetida (`duplicate`) y no se toca nada más. La inserción es la
   barrera de concurrencia: una segunda entrega simultánea espera a la primera;
2. bloquea solo la fila del usuario afectado y compara versiones;
3. aplica el cambio o lo registra como superado (`obsolete`), y confirma ambas cosas juntas.

Reglas por vista:

- `usuario.registrado.v1` y `usuario.actualizado.v1` (HU002) → `user_directory`, que
  conserva la secuencia de versiones de la cuenta: solo se aplica la versión siguiente (la 1
  en una fila ausente; una edición, sobre la anterior); una versión igual o anterior es
  obsoleta y un hueco revierte la transacción (`ProjectionGap`): el mensaje no se confirma y
  Pub/Sub lo reintenta hasta que llegue la anterior o termine en la DLQ. Si el correo nuevo
  todavía figura en otra fila (otra cuenta lo liberó y su edición no se ha proyectado), la
  unicidad del directorio revierte la transacción (`DirectoryEmailInUse`) y se reintenta.
- `perfil.*_actualizado.v1` → sección de `athlete_profile_view` (ADR-034): el evento trae el
  estado completo, así que se aplica si su versión es mayor que la proyectada (`null`
  cuenta como 0), crea la fila si no existe y deja intactas las otras secciones.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from types import TracebackType
from typing import Any, NoReturn, Protocol, Self
from uuid import UUID

from users_management.application.messages import (
    ProfileSectionUpdatedEvent,
    UserRegisteredEvent,
    UsersEvent,
    UserUpdatedEvent,
)
from users_management.application.ports import Clock
from users_management.domain.profile.events import ProfileSection

DIRECTORY_PROJECTION = "user_directory"
PROFILE_PROJECTION = "athlete_profile_view"

_logger = logging.getLogger("sportapp.projector")


class ProjectionOutcome(StrEnum):
    APPLIED = "applied"
    DUPLICATE = "duplicate"
    OBSOLETE = "obsolete"


class ProjectionGap(Exception):
    """Llegó una versión posterior a la siguiente esperada; se reintenta más tarde."""


class DirectoryEmailInUse(Exception):
    """El correo proyectado sigue en otra fila del directorio; se reintenta más tarde."""


@dataclass(frozen=True)
class DirectoryEntry:
    """Fila de `user_directory`: identidad mínima y nombre visible (HU011), sin credenciales."""

    user_id: UUID
    full_name: str
    email: str
    actor_type: str
    status: str
    aggregate_version: int

    def __repr__(self) -> str:
        return f"DirectoryEntry(user_id={self.user_id}, version={self.aggregate_version})"


class ProjectionUnitOfWork(Protocol):
    """Transacción de la base de lectura; al salir sin `commit` se revierte todo."""

    def __enter__(self) -> Self: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...

    def claim_event(
        self,
        event_id: UUID,
        projection_name: str,
        aggregate_id: UUID,
        aggregate_version: int,
        processed_at: datetime,
    ) -> bool:
        """Inserta el `event_id` en `processed_event`; `False` si ya estaba registrado."""
        ...

    def directory_version(self, user_id: UUID) -> int | None:
        """Versión proyectada del usuario, bloqueando su fila (`FOR UPDATE`) si existe."""
        ...

    def add_directory_entry(self, entry: DirectoryEntry, projected_at: datetime) -> bool:
        """Inserta la fila; `False` si otra transacción la creó primero."""
        ...

    def update_directory_entry(self, entry: DirectoryEntry, projected_at: datetime) -> None:
        """Reemplaza nombre, correo, actor, estado y versión de una fila existente (HU002).

        `DirectoryEmailInUse` si el correo está en otra fila del directorio.
        """
        ...

    def profile_section_version(self, user_id: UUID, section: ProfileSection) -> int | None:
        """Versión proyectada de la sección, bloqueando la fila del usuario si existe."""
        ...

    def save_profile_section(
        self,
        user_id: UUID,
        section: ProfileSection,
        version: int,
        document: dict[str, Any],
        projected_at: datetime,
    ) -> bool:
        """Crea la fila o reemplaza la sección solo si `version` es mayor que la proyectada.

        Devuelve `False` si no aplicó (otra transacción dejó una versión igual o mayor).
        """
        ...

    def commit(self) -> None: ...


class UsersProjector:
    """Despacha cada evento validado a su vista; devuelve el resultado para el log."""

    def __init__(self, *, unit_of_work: Callable[[], ProjectionUnitOfWork], clock: Clock) -> None:
        self._unit_of_work = unit_of_work
        self._clock = clock

    def __call__(self, event: UsersEvent) -> ProjectionOutcome:
        if isinstance(event, UserRegisteredEvent):
            return self._project_registration(event)
        if isinstance(event, UserUpdatedEvent):
            return self._project_update(event)
        return self._project_profile_section(event)

    def _project_registration(self, event: UserRegisteredEvent) -> ProjectionOutcome:
        now = self._clock.now()
        with self._unit_of_work() as unit_of_work:
            if not unit_of_work.claim_event(
                event.event_id,
                DIRECTORY_PROJECTION,
                event.user_id,
                event.aggregate_version,
                now,
            ):
                return ProjectionOutcome.DUPLICATE
            projected = unit_of_work.directory_version(event.user_id)
            if projected is not None and projected >= event.aggregate_version:
                outcome = ProjectionOutcome.OBSOLETE
            elif projected is None and event.aggregate_version == 1:
                inserted = unit_of_work.add_directory_entry(
                    DirectoryEntry(
                        user_id=event.user_id,
                        full_name=event.full_name,
                        email=event.email,
                        actor_type=event.actor_type,
                        status=event.status,
                        aggregate_version=event.aggregate_version,
                    ),
                    now,
                )
                # Si otra entrega de la misma versión creó la fila primero, ya está aplicada.
                outcome = ProjectionOutcome.APPLIED if inserted else ProjectionOutcome.OBSOLETE
            else:
                # El registro siempre es la versión 1 sobre una fila ausente: cualquier otra
                # secuencia es un hueco que no se adivina (CMP-001).
                _version_gap(event, projected)
            unit_of_work.commit()
        return outcome

    def _project_update(self, event: UserUpdatedEvent) -> ProjectionOutcome:
        now = self._clock.now()
        with self._unit_of_work() as unit_of_work:
            if not unit_of_work.claim_event(
                event.event_id,
                DIRECTORY_PROJECTION,
                event.user_id,
                event.aggregate_version,
                now,
            ):
                return ProjectionOutcome.DUPLICATE
            projected = unit_of_work.directory_version(event.user_id)
            if projected is not None and projected >= event.aggregate_version:
                outcome = ProjectionOutcome.OBSOLETE
            elif projected == event.aggregate_version - 1:
                unit_of_work.update_directory_entry(
                    DirectoryEntry(
                        user_id=event.user_id,
                        full_name=event.full_name,
                        email=event.email,
                        actor_type=event.actor_type,
                        status=event.status,
                        aggregate_version=event.aggregate_version,
                    ),
                    now,
                )
                outcome = ProjectionOutcome.APPLIED
            else:
                # Falta el registro o una edición anterior: no se adivina el estado intermedio.
                _version_gap(event, projected)
            unit_of_work.commit()
        return outcome

    def _project_profile_section(self, event: ProfileSectionUpdatedEvent) -> ProjectionOutcome:
        now = self._clock.now()
        with self._unit_of_work() as unit_of_work:
            if not unit_of_work.claim_event(
                event.event_id,
                PROFILE_PROJECTION,
                event.user_id,
                event.aggregate_version,
                now,
            ):
                return ProjectionOutcome.DUPLICATE
            projected = unit_of_work.profile_section_version(event.user_id, event.section)
            if (projected or 0) >= event.aggregate_version:
                outcome = ProjectionOutcome.OBSOLETE
            else:
                applied = unit_of_work.save_profile_section(
                    event.user_id, event.section, event.aggregate_version, event.document, now
                )
                outcome = ProjectionOutcome.APPLIED if applied else ProjectionOutcome.OBSOLETE
            unit_of_work.commit()
        return outcome


def _version_gap(event: UserRegisteredEvent | UserUpdatedEvent, projected: int | None) -> NoReturn:
    _logger.warning(
        "projector.version_gap",
        extra={
            "fields": {
                "event_id": str(event.event_id),
                "aggregate_id": str(event.user_id),
                "projected_version": projected,
                "aggregate_version": event.aggregate_version,
            }
        },
    )
    raise ProjectionGap()
