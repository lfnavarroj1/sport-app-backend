"""HU031: solicitud de sincronización manual (ADR-042 §8, CA4).

`POST /v1/me/integrations/strava/syncs` con `Idempotency-Key` (24 horas):
1. `athlete` con `integrations.strava.connect` (fuera de la transacción) y Strava
   configurado (sin él el worker no podría correr, R).
2. En una transacción con `external_account` bloqueada (`FOR UPDATE`):
   - la misma clave vigente → la misma corrida con su estado actual (`replayed`);
   - sin cuenta o no `ACTIVE` → `409 integration_not_linked` (R);
   - otra corrida `QUEUED`/`RUNNING` → `409 sync_in_progress`;
   - si no, una corrida `QUEUED` y la clave, en el mismo COMMIT.
   El índice único parcial de `sync_run` garantiza una sola corrida pendiente aunque dos
   réplicas compitan; la clave la protege su clave primaria.

Auditoría `audit.strava_sync_requested` con `user_id`, `result` y `sync_run_id`.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

from third_party_management.application.access import (
    require_configured,
    require_strava_capability,
)
from third_party_management.application.errors import (
    CapabilitiesUnavailable,
    CapabilityRequired,
    IdempotencyKeyConflict,
    IntegrationNotLinked,
    PermissionRequired,
    StravaNotConfigured,
    SyncInProgress,
    SyncRunConflict,
)
from third_party_management.application.ports import (
    Caller,
    CapabilityChecker,
    Clock,
    IdempotencyRecord,
    StravaContext,
    UnitOfWork,
)
from third_party_management.domain.synchronization import SyncRun

REQUEST_SYNC_OPERATION = "request_strava_sync"
IDEMPOTENCY_TTL = timedelta(hours=24)

_audit = logging.getLogger("sportapp.audit")

_REJECTIONS = (
    PermissionRequired,
    CapabilityRequired,
    CapabilitiesUnavailable,
    StravaNotConfigured,
    IntegrationNotLinked,
    SyncInProgress,
)


@dataclass(frozen=True)
class SyncRequestResult:
    run: SyncRun
    replayed: bool = False


class RequestSync:
    def __init__(
        self,
        *,
        unit_of_work: Callable[[], UnitOfWork],
        capabilities: CapabilityChecker,
        strava: StravaContext | None,
        clock: Clock,
    ) -> None:
        self._unit_of_work = unit_of_work
        self._capabilities = capabilities
        self._strava = strava
        self._clock = clock

    def __call__(
        self, caller: Caller, idempotency_key: UUID, correlation_id: UUID | None
    ) -> SyncRequestResult:
        try:
            result = self._execute(caller, idempotency_key, correlation_id)
        except _REJECTIONS as error:
            _audit.info(
                "audit.strava_sync_requested",
                extra={
                    "fields": {
                        "user_id": str(caller.user_id),
                        "result": "rejected",
                        "reason": error.code,
                    }
                },
            )
            raise
        _audit.info(
            "audit.strava_sync_requested",
            extra={
                "fields": {
                    "user_id": str(caller.user_id),
                    "result": "replayed" if result.replayed else "accepted",
                    "sync_run_id": str(result.run.sync_run_id),
                }
            },
        )
        return result

    def _execute(self, caller: Caller, key: UUID, correlation_id: UUID | None) -> SyncRequestResult:
        require_strava_capability(caller, self._capabilities)
        require_configured(self._strava)
        try:
            return self._request(caller.user_id, key, correlation_id)
        except (IdempotencyKeyConflict, SyncRunConflict):
            # Otra solicitud confirmó primero: con la misma clave se devuelve su corrida;
            # con otra, la corrida pendiente que dejó la ganadora produce `sync_in_progress`.
            return self._request(caller.user_id, key, correlation_id)

    def _request(self, user_id: UUID, key: UUID, correlation_id: UUID | None) -> SyncRequestResult:
        now = self._clock.now()
        with self._unit_of_work() as unit_of_work:
            account = unit_of_work.get_account(user_id, for_update=True)
            record = unit_of_work.find_idempotency(user_id, REQUEST_SYNC_OPERATION, key)
            if record is not None:
                previous = unit_of_work.get_run(record.result_id)
                if record.created_at + IDEMPOTENCY_TTL > now and previous is not None:
                    return SyncRequestResult(previous, replayed=True)
                unit_of_work.delete_idempotency(user_id, REQUEST_SYNC_OPERATION, key)
            if account is None or not account.is_active:
                raise IntegrationNotLinked()
            if unit_of_work.pending_runs(user_id):
                raise SyncInProgress()
            run = SyncRun.queue(user_id, now, correlation_id)
            unit_of_work.add_run(run)
            unit_of_work.add_idempotency(
                IdempotencyRecord(
                    user_id=user_id,
                    operation=REQUEST_SYNC_OPERATION,
                    key=key,
                    result_id=run.sync_run_id,
                    created_at=now,
                )
            )
            unit_of_work.commit()
        return SyncRequestResult(run)
