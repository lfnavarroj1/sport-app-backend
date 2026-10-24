"""Asociación del deportista a un servicio médico deportivo (HU017, ADR-038).

- Solo se asocia un servicio `PUBLISHED` de categoría `SPORTS_MEDICINE`.
- Nace `ACTIVE`, versión 1; como máximo una activa por deportista y servicio (el índice
  único parcial de la base lo garantiza también ante solicitudes concurrentes).
- Desasociar la deja `INACTIVE` con `ended_at` y sube la versión una vez; repetirlo no
  cambia nada. La relación se conserva como historial.
- Asociar solo crea la relación: no comparte datos de salud, no contrata, no reserva ni
  paga (ADR-027).
"""

from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from complementary_services.domain.catalog.offer import Category, ServiceOffer, ServiceStatus


class AssociationStatus(StrEnum):
    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"


def is_associable(offer: ServiceOffer | None) -> bool:
    """Servicio publicado de medicina deportiva; un borrador o de otra categoría no lo es."""
    return (
        offer is not None
        and offer.status is ServiceStatus.PUBLISHED
        and offer.category is Category.SPORTS_MEDICINE
    )


@dataclass(frozen=True)
class ServiceAssociation:
    association_id: UUID
    service_id: UUID
    athlete_id: UUID
    status: AssociationStatus
    associated_at: datetime
    version: int
    ended_at: datetime | None = None

    @classmethod
    def associate(
        cls, *, association_id: UUID, offer: ServiceOffer, athlete_id: UUID, now: datetime
    ) -> "ServiceAssociation":
        if not is_associable(offer):
            raise ValueError("Solo un servicio médico deportivo publicado se puede asociar.")
        return cls(
            association_id=association_id,
            service_id=offer.service_id,
            athlete_id=athlete_id,
            status=AssociationStatus.ACTIVE,
            associated_at=now,
            version=1,
        )

    @property
    def is_active(self) -> bool:
        return self.status is AssociationStatus.ACTIVE

    def end(self, now: datetime) -> "ServiceAssociation":
        """`ACTIVE` → `INACTIVE` con `ended_at`; una ya inactiva se devuelve sin cambios."""
        if not self.is_active:
            return self
        return replace(
            self, status=AssociationStatus.INACTIVE, ended_at=now, version=self.version + 1
        )
