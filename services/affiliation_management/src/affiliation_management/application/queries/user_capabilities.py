"""Instantánea de capacidades vigentes de un usuario (contrato interno de ADR-024).

Sin suscripción activa responde `NONE` y ninguna capacidad: Afiliación nunca
concede `FREE` por suposición. El BFF decide cómo presentarlo (para un `athlete`
recién registrado, `PROVISIONING`).
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from affiliation_management.application.ports import CapabilitiesReader, Clock


class SnapshotStatus(StrEnum):
    ACTIVE = "ACTIVE"
    NONE = "NONE"


@dataclass(frozen=True)
class UserCapabilities:
    user_id: UUID
    subscription_status: SnapshotStatus
    plan_code: str | None
    plan_version: int | None
    catalog_version: str | None
    capabilities: tuple[str, ...]
    evaluated_at: datetime


class GetUserCapabilities:
    def __init__(self, *, reader: CapabilitiesReader, clock: Clock) -> None:
        self._reader = reader
        self._clock = clock

    def __call__(self, user_id: UUID) -> UserCapabilities:
        snapshot = self._reader.active_snapshot(user_id)
        now = self._clock.now()
        if snapshot is None:
            return UserCapabilities(
                user_id=user_id,
                subscription_status=SnapshotStatus.NONE,
                plan_code=None,
                plan_version=None,
                catalog_version=None,
                capabilities=(),
                evaluated_at=now,
            )
        return UserCapabilities(
            user_id=user_id,
            subscription_status=SnapshotStatus.ACTIVE,
            plan_code=snapshot.plan.plan_code,
            plan_version=snapshot.plan.version,
            catalog_version=snapshot.plan.catalog_version,
            capabilities=tuple(sorted(snapshot.capabilities)),
            evaluated_at=now,
        )
