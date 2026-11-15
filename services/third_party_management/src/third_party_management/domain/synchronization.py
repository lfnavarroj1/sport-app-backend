"""Corridas de sincronización manual con Strava (HU031, ADR-042 §8-§9).

Una sola corrida `QUEUED`/`RUNNING` por deportista (índice único parcial). El worker la
pasa a `RUNNING`, consulta Strava sin transacción abierta y la cierra en `SUCCEEDED`,
`FAILED` o `RATE_LIMITED` con el `error_code` del catálogo (R, enum del OpenAPI):

- `rate_limited` (`RATE_LIMITED`): `429` o cuota agotada según `X-RateLimit-Usage`;
- `provider_unavailable`: `5xx`/timeout tras agotar los reintentos;
- `circuit_open`: circuito abierto, no se llamó a Strava;
- `token_expired`: `401` tras renovar o renovación rechazada (la cuenta pasa a `EXPIRED`);
- `insufficient_scope`: `403`;
- `provider_rejected`: otro `4xx` (p. ej. `404`);
- `invalid_response`: página ilegible (la corrida se detiene);
- `invalid_activity`: al menos una actividad omitida por datos parciales (las válidas sí se
  publican);
- `integration_revoked`: el deportista desvinculó la cuenta.
"""

from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

# ADR-042 §9: hasta 3 páginas de 50 actividades por corrida.
MAX_PAGES = 3
PAGE_SIZE = 50


class SyncStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    RATE_LIMITED = "RATE_LIMITED"


PENDING_STATUSES = (SyncStatus.QUEUED, SyncStatus.RUNNING)


class SyncErrorCode(StrEnum):
    RATE_LIMITED = "rate_limited"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    CIRCUIT_OPEN = "circuit_open"
    TOKEN_EXPIRED = "token_expired"  # noqa: S105  # nosec B105 - código de error, no secreto
    INSUFFICIENT_SCOPE = "insufficient_scope"
    PROVIDER_REJECTED = "provider_rejected"
    INVALID_RESPONSE = "invalid_response"
    INVALID_ACTIVITY = "invalid_activity"
    INTEGRATION_REVOKED = "integration_revoked"


@dataclass(frozen=True)
class SyncRun:
    sync_run_id: UUID
    user_id: UUID
    status: SyncStatus
    imported_count: int
    error_code: SyncErrorCode | None
    requested_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    correlation_id: UUID | None

    @property
    def is_pending(self) -> bool:
        return self.status in PENDING_STATUSES

    @classmethod
    def queue(cls, user_id: UUID, now: datetime, correlation_id: UUID | None) -> "SyncRun":
        return cls(
            sync_run_id=uuid4(),
            user_id=user_id,
            status=SyncStatus.QUEUED,
            imported_count=0,
            error_code=None,
            requested_at=now,
            started_at=None,
            finished_at=None,
            correlation_id=correlation_id,
        )

    def cancel(self, now: datetime) -> "SyncRun":
        """CA9: al desvincular, una corrida pendiente termina `FAILED` `integration_revoked`."""
        return replace(
            self,
            status=SyncStatus.FAILED,
            error_code=SyncErrorCode.INTEGRATION_REVOKED,
            finished_at=now,
        )

    def finish(
        self, error_code: SyncErrorCode | None, imported_count: int, now: datetime
    ) -> "SyncRun":
        if error_code is None:
            status = SyncStatus.SUCCEEDED
        elif error_code is SyncErrorCode.RATE_LIMITED:
            status = SyncStatus.RATE_LIMITED
        else:
            status = SyncStatus.FAILED
        return replace(
            self,
            status=status,
            error_code=error_code,
            imported_count=imported_count,
            finished_at=now,
        )
