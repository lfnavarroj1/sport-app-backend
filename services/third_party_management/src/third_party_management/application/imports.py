"""HU031: corrida de importación de `terceros-sync-worker` (ADR-042 §8-§11, CA4-CA7).

1. Reclama una corrida (`FOR UPDATE SKIP LOCKED` + arrendamiento) y confirma `RUNNING`.
2. Lee la cuenta y la última actividad importada en una transacción corta, sin bloqueos.
3. Sin transacción abierta: renueva el token si le faltan menos de 5 minutos y pide hasta 3
   páginas de 50 actividades con `after` = inicio de la última importada - 1 día (sin
   importadas, sin `after`). Un `401` renueva una vez y repite la página; otro `401` o una
   renovación rechazada dejan la cuenta `EXPIRED` (`token_expired`). `429` o la cuota
   agotada detienen la corrida en `RATE_LIMITED`; las demás fallas la terminan `FAILED`
   con el `error_code` del catálogo. Las páginas ya recibidas sí se importan.
4. Normaliza cada actividad (`strava-activity.v1`): una incompleta se omite, se registra
   `sync.activity_skipped` (identificador y motivo, nunca datos del proveedor) y la corrida
   termina `FAILED` `invalid_activity` si no hubo otra falla; las válidas sí se publican.
5. En una transacción final, solo si la corrida sigue `RUNNING` y es de este worker (una
   desvinculación concurrente la canceló: no se sobrescribe nada): tokens renovados,
   `imported_activity` y `actividad.importada.v1` en la outbox en el mismo COMMIT, estado
   final e `imported_count` (= eventos encolados).
"""

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from third_party_management.application.errors import ProviderFailure, ProviderFailureKind
from third_party_management.application.ports import (
    ActivityPage,
    Clock,
    OutboxMessage,
    ProviderTokens,
    StravaContext,
    SyncRunClaimer,
    UnitOfWork,
)
from third_party_management.domain.activities import (
    MAPPING_VERSION,
    PROVIDER,
    ImportedActivity,
    InvalidActivity,
    NormalizedActivity,
    next_import,
    normalize_activity,
)
from third_party_management.domain.connections import (
    REFRESH_MARGIN,
    AccountStatus,
    ExternalAccount,
    SealedTokens,
)
from third_party_management.domain.instants import utc_instant
from third_party_management.domain.synchronization import (
    MAX_PAGES,
    PAGE_SIZE,
    SyncErrorCode,
    SyncRun,
)

EVENT_TYPE = "actividad.importada.v1"
AGGREGATE_TYPE = "external_activity"
SCHEMA_VERSION = 1
AFTER_OVERLAP = timedelta(days=1)

_logger = logging.getLogger("sportapp.sync")

ERROR_BY_FAILURE: dict[ProviderFailureKind, SyncErrorCode] = {
    ProviderFailureKind.RATE_LIMITED: SyncErrorCode.RATE_LIMITED,
    ProviderFailureKind.UNAVAILABLE: SyncErrorCode.PROVIDER_UNAVAILABLE,
    ProviderFailureKind.CIRCUIT_OPEN: SyncErrorCode.CIRCUIT_OPEN,
    ProviderFailureKind.UNAUTHORIZED: SyncErrorCode.TOKEN_EXPIRED,
    ProviderFailureKind.FORBIDDEN: SyncErrorCode.INSUFFICIENT_SCOPE,
    ProviderFailureKind.REJECTED: SyncErrorCode.PROVIDER_REJECTED,
    ProviderFailureKind.INVALID_RESPONSE: SyncErrorCode.INVALID_RESPONSE,
}
# Al renovar, un rechazo del proveedor (400/401) significa que la autorización ya no vale.
_REFRESH_REJECTIONS = (ProviderFailureKind.UNAUTHORIZED, ProviderFailureKind.REJECTED)


def activity_payload(activity: NormalizedActivity, user_id: UUID, version: int) -> dict[str, Any]:
    """Payload exacto de `actividad.importada.v1`: sin tokens ni JSON de Strava."""
    return {
        "activity_id": str(activity.activity_id),
        "user_id": str(user_id),
        "provider": PROVIDER,
        "external_activity_id": activity.external_activity_id,
        "sport": activity.sport.value,
        "name": activity.name,
        "started_at": utc_instant(activity.started_at),
        "ended_at": utc_instant(activity.ended_at),
        "time_zone": activity.time_zone,
        "duration_seconds": activity.duration_seconds,
        "moving_seconds": activity.moving_seconds,
        "distance_meters": activity.distance_meters,
        "elevation_gain_meters": activity.elevation_gain_meters,
        "mapping_version": MAPPING_VERSION,
        "version": version,
    }


def activity_event(
    activity: NormalizedActivity,
    imported: ImportedActivity,
    run: SyncRun,
    now: datetime,
) -> OutboxMessage:
    return OutboxMessage(
        event_id=uuid4(),
        event_type=EVENT_TYPE,
        schema_version=SCHEMA_VERSION,
        aggregate_type=AGGREGATE_TYPE,
        aggregate_id=imported.activity_id,
        aggregate_version=imported.version,
        occurred_at=now,
        correlation_id=run.correlation_id,
        causation_id=str(run.sync_run_id),
        payload=activity_payload(activity, imported.user_id, imported.version),
    )


@dataclass
class _Fetch:
    """Resultado de consultar al proveedor, antes de escribir nada."""

    records: list[object] = field(default_factory=list)
    error: SyncErrorCode | None = None
    renewed: ProviderTokens | None = None
    expired: bool = False
    pages: int = 0


@dataclass(frozen=True)
class SyncOutcome:
    sync_run_id: UUID
    status: str
    error_code: SyncErrorCode | None
    imported_count: int
    skipped_count: int
    cancelled: bool = False


class RunSync:
    def __init__(
        self,
        *,
        claimer: SyncRunClaimer,
        unit_of_work: Callable[[], UnitOfWork],
        strava: StravaContext,
        clock: Clock,
        worker_id: str,
        lease_seconds: float,
    ) -> None:
        self._claimer = claimer
        self._unit_of_work = unit_of_work
        self._strava = strava
        self._clock = clock
        self._worker_id = worker_id
        self._lease_seconds = lease_seconds

    def __call__(self) -> SyncOutcome | None:
        """Procesa una corrida; `None` si no había ninguna pendiente."""
        run = self._claimer.claim(self._worker_id, self._lease_seconds)
        if run is None:
            return None
        started = time.perf_counter()
        _logger.info("sync.started", extra={"fields": self._run_fields(run)})
        with self._unit_of_work() as unit_of_work:
            account = unit_of_work.get_account(run.user_id)
            last_start = unit_of_work.last_imported_start(run.user_id)
        if account is None or account.tokens is None or not account.is_active:
            fetch = _Fetch(error=_inactive_error(account))
        else:
            fetch = self._fetch(account.user_id, account.tokens, last_start)
        activities, skipped = self._normalize(run, fetch.records)
        outcome = self._complete(run, fetch, activities, skipped)
        _logger.info(
            "sync.finished",
            extra={
                "fields": {
                    **self._run_fields(run),
                    "status": outcome.status,
                    "error_code": outcome.error_code.value if outcome.error_code else None,
                    "imported_count": outcome.imported_count,
                    "skipped_count": outcome.skipped_count,
                    "pages": fetch.pages,
                    "cancelled": outcome.cancelled,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                }
            },
        )
        return outcome

    @staticmethod
    def _run_fields(run: SyncRun) -> dict[str, Any]:
        return {"sync_run_id": str(run.sync_run_id), "user_id": str(run.user_id)}

    # ------------------------------------------------------------- proveedor

    def _fetch(self, user_id: UUID, sealed: SealedTokens, last_start: datetime | None) -> _Fetch:
        fetch = _Fetch()
        try:
            tokens = self._strava.sealer.open(user_id, sealed)
        except ValueError:
            # Un token que no se puede descifrar equivale a una autorización inválida.
            fetch.expired, fetch.error = True, SyncErrorCode.TOKEN_EXPIRED
            return fetch
        if tokens.expires_at - self._clock.now() < REFRESH_MARGIN:
            renewed = self._renew(fetch, tokens)
            if renewed is None:
                return fetch
            tokens = renewed
        after = last_start - AFTER_OVERLAP if last_start is not None else None
        for page in range(1, MAX_PAGES + 1):
            outcome = self._page(fetch, tokens, after, page)
            if outcome is None:
                return fetch
            tokens, size, exhausted = outcome
            if exhausted:
                fetch.error = SyncErrorCode.RATE_LIMITED
                return fetch
            if size < PAGE_SIZE:
                return fetch
        return fetch

    def _page(
        self, fetch: _Fetch, tokens: ProviderTokens, after: datetime | None, page: int
    ) -> tuple[ProviderTokens, int, bool] | None:
        """Una página con un solo reintento tras renovar por `401`; `None` si se detiene."""
        try:
            result = self._list(tokens, after, page)
        except ProviderFailure as failure:
            if failure.kind is not ProviderFailureKind.UNAUTHORIZED or fetch.renewed is not None:
                self._stop(fetch, failure)
                return None
            renewed = self._renew(fetch, tokens)
            if renewed is None:
                return None
            tokens = renewed
            try:
                result = self._list(tokens, after, page)
            except ProviderFailure as retry_failure:
                self._stop(fetch, retry_failure)
                return None
        fetch.pages += 1
        fetch.records.extend(result.records)
        return tokens, len(result.records), result.quota_exhausted

    def _list(self, tokens: ProviderTokens, after: datetime | None, page: int) -> ActivityPage:
        return self._strava.gateway.list_activities(
            tokens.access_token, after=after, page=page, per_page=PAGE_SIZE
        )

    def _renew(self, fetch: _Fetch, tokens: ProviderTokens) -> ProviderTokens | None:
        try:
            renewed = self._strava.gateway.refresh(tokens.refresh_token)
        except ProviderFailure as failure:
            if failure.kind in _REFRESH_REJECTIONS:
                fetch.expired, fetch.error = True, SyncErrorCode.TOKEN_EXPIRED
            else:
                fetch.error = ERROR_BY_FAILURE[failure.kind]
            return None
        fetch.renewed = renewed
        return renewed

    @staticmethod
    def _stop(fetch: _Fetch, failure: ProviderFailure) -> None:
        fetch.error = ERROR_BY_FAILURE[failure.kind]
        if failure.kind is ProviderFailureKind.UNAUTHORIZED:
            fetch.expired = True
        if failure.kind is ProviderFailureKind.RATE_LIMITED:
            _logger.warning(
                "sync.rate_limited",
                extra={"fields": {"retry_after_seconds": failure.retry_after_seconds}},
            )

    # ---------------------------------------------------------- normalización

    def _normalize(
        self, run: SyncRun, records: list[object]
    ) -> tuple[list[NormalizedActivity], int]:
        activities: list[NormalizedActivity] = []
        skipped = 0
        for record in records:
            try:
                activities.append(normalize_activity(record))
            except InvalidActivity as invalid:
                skipped += 1
                _logger.warning(
                    "sync.activity_skipped",
                    extra={
                        "fields": {
                            **self._run_fields(run),
                            "external_activity_id": invalid.external_activity_id,
                            "reason": invalid.reason.value,
                        }
                    },
                )
        return activities, skipped

    # ---------------------------------------------------------------- cierre

    def _complete(
        self,
        run: SyncRun,
        fetch: _Fetch,
        activities: list[NormalizedActivity],
        skipped: int,
    ) -> SyncOutcome:
        error = fetch.error
        if error is None and skipped:
            error = SyncErrorCode.INVALID_ACTIVITY
        now = self._clock.now()
        with self._unit_of_work() as unit_of_work:
            owned = unit_of_work.lock_owned_run(run.sync_run_id, self._worker_id)
            if owned is None:
                # Cancelada por una desvinculación (o reclamada por otro worker tras vencer
                # el arrendamiento): no se escribe nada.
                return SyncOutcome(run.sync_run_id, "CANCELLED", None, 0, skipped, True)
            account = unit_of_work.get_account(run.user_id, for_update=True)
            if account is not None and account.is_active:
                if fetch.expired:
                    unit_of_work.save_account(account.expire(now))
                elif fetch.renewed is not None:
                    sealed = self._strava.sealer.seal(run.user_id, fetch.renewed)
                    unit_of_work.save_account(account.with_tokens(sealed, now))
            imported = self._import(unit_of_work, owned, activities, now)
            finished = owned.finish(error, imported, now)
            unit_of_work.save_run(finished)
            unit_of_work.commit()
        return SyncOutcome(
            run.sync_run_id, finished.status.value, finished.error_code, imported, skipped
        )

    def _import(
        self,
        unit_of_work: UnitOfWork,
        run: SyncRun,
        activities: list[NormalizedActivity],
        now: datetime,
    ) -> int:
        published = 0
        for activity in activities:
            current = unit_of_work.get_imported(activity.external_activity_id)
            if current is not None and current.user_id != run.user_id:
                _logger.warning(
                    "sync.activity_skipped",
                    extra={
                        "fields": {
                            **self._run_fields(run),
                            "external_activity_id": activity.external_activity_id,
                            "reason": "owned_by_other_user",
                        }
                    },
                )
                continue
            imported = next_import(current, activity, run.user_id, now)
            if imported is None:
                continue
            unit_of_work.save_imported(imported, new=current is None)
            unit_of_work.add_outbox(activity_event(activity, imported, run, now))
            published += 1
        return published


def _inactive_error(account: ExternalAccount | None) -> SyncErrorCode:
    if account is not None and account.status is AccountStatus.EXPIRED:
        return SyncErrorCode.TOKEN_EXPIRED
    return SyncErrorCode.INTEGRATION_REVOKED
