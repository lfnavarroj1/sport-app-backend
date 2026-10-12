"""Autorización deportista → profesional (decisión del 2026-10-12 para HU010).

El deportista autoriza previamente a un profesional; solo entonces ese profesional
puede crear y asignarle planes (CA1, CA2). La autorización puede revocarse; una
revocada se reactiva si el deportista vuelve a autorizar.
"""

from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from uuid import UUID


class AuthorizationStatus(StrEnum):
    ACTIVE = "ACTIVE"
    REVOKED = "REVOKED"


class SelfAuthorization(Exception):
    """Un usuario no puede autorizarse a sí mismo."""

    code = "validation_error"


@dataclass(frozen=True)
class TrainingAuthorization:
    athlete_id: UUID
    professional_id: UUID
    status: AuthorizationStatus
    granted_at: datetime
    revoked_at: datetime | None
    version: int

    @property
    def is_active(self) -> bool:
        return self.status is AuthorizationStatus.ACTIVE

    @classmethod
    def grant(
        cls, *, athlete_id: UUID, professional_id: UUID, now: datetime
    ) -> "TrainingAuthorization":
        if athlete_id == professional_id:
            raise SelfAuthorization()
        return cls(
            athlete_id=athlete_id,
            professional_id=professional_id,
            status=AuthorizationStatus.ACTIVE,
            granted_at=now,
            revoked_at=None,
            version=1,
        )

    def reactivate(self, now: datetime) -> "TrainingAuthorization":
        return replace(
            self,
            status=AuthorizationStatus.ACTIVE,
            granted_at=now,
            revoked_at=None,
            version=self.version + 1,
        )

    def revoke(self, now: datetime) -> "TrainingAuthorization":
        return replace(
            self, status=AuthorizationStatus.REVOKED, revoked_at=now, version=self.version + 1
        )
