"""`terceros-sync-worker`: ejecuta las sincronizaciones manuales con Strava (HU031, ADR-042 §8).

Ejecución: `python -m third_party_management.workers.sync_worker`.

Cada ciclo reclama una corrida con `FOR UPDATE SKIP LOCKED` y arrendamiento (COMMIT antes de
llamar a Strava), consulta el proveedor sin transacción abierta y cierra la corrida con las
actividades importadas y sus eventos en la outbox en una sola transacción (ver
`application.imports.RunSync`). El circuito del proveedor es por proceso. Sin corridas, la
espera crece hasta `SYNC_IDLE_POLL_MAX_SECONDS`.
"""

import logging
import signal
import threading

import httpx
from sqlalchemy.orm import Session, sessionmaker

from sportapp_platform.config import load_settings
from sportapp_platform.database import create_database_engine
from sportapp_platform.logging import configure_logging
from third_party_management.application.imports import RunSync
from third_party_management.application.ports import StravaContext
from third_party_management.infrastructure.clock import SystemClock
from third_party_management.infrastructure.oauth_state import HmacStateCodec
from third_party_management.infrastructure.persistence.sync_claims import (
    SqlAlchemySyncRunClaimer,
)
from third_party_management.infrastructure.persistence.unit_of_work import SqlAlchemyUnitOfWork
from third_party_management.infrastructure.strava.circuit import CircuitBreaker
from third_party_management.infrastructure.strava.client import (
    STRAVA_TIMEOUT,
    StravaCredentials,
    StravaHttpGateway,
)
from third_party_management.infrastructure.token_cipher import (
    AesGcmTokenSealer,
    decode_key,
    derive_state_key,
)
from third_party_management.settings import SyncWorkerSettings

_logger = logging.getLogger("sportapp.sync")


def build_run_sync(
    settings: SyncWorkerSettings,
    sessions: sessionmaker[Session],
    strava_http: httpx.Client,
    breaker: CircuitBreaker | None = None,
) -> RunSync:
    """El worker no inicia vinculaciones: `redirect_uri` y `consent_version` no se usan."""
    key = decode_key(settings.strava_token_key.get_secret_value())
    gateway = StravaHttpGateway(
        strava_http,
        StravaCredentials(
            client_id=settings.strava_client_id,
            client_secret=settings.strava_client_secret.get_secret_value(),
            redirect_uri="",
        ),
        breaker=breaker,
    )
    return RunSync(
        claimer=SqlAlchemySyncRunClaimer(sessions),
        unit_of_work=lambda: SqlAlchemyUnitOfWork(sessions),
        strava=StravaContext(
            gateway=gateway,
            sealer=AesGcmTokenSealer(key),
            states=HmacStateCodec(derive_state_key(key)),
            consent_version="",
        ),
        clock=SystemClock(),
        worker_id=settings.worker_id,
        lease_seconds=settings.sync_lease_seconds,
    )


class SyncWorker:
    def __init__(self, run_sync: RunSync, settings: SyncWorkerSettings) -> None:
        self._run_sync = run_sync
        self._settings = settings

    def run_once(self) -> bool:
        """Procesa una corrida; `False` si no había ninguna pendiente."""
        return self._run_sync() is not None

    def run_forever(self, stop: threading.Event) -> None:
        idle_wait = self._settings.sync_busy_poll_seconds
        while not stop.is_set():
            try:
                processed = self.run_once()
            except Exception:
                _logger.exception("sync.cycle_failed")
                processed = False
            if processed:
                wait = self._settings.sync_busy_poll_seconds
                idle_wait = wait
            else:
                wait = idle_wait
                idle_wait = min(idle_wait * 2, self._settings.sync_idle_poll_max_seconds)
            stop.wait(wait)


def main() -> None:
    settings = load_settings(SyncWorkerSettings)
    configure_logging(
        service="third-party-management-sync-worker",
        environment=settings.app_env,
        version=settings.service_version,
        level=settings.log_level,
    )
    engine = create_database_engine(settings.database_url, settings)
    strava_http = httpx.Client(base_url=settings.strava_base_url, timeout=STRAVA_TIMEOUT)
    worker = SyncWorker(
        build_run_sync(settings, sessionmaker(engine, expire_on_commit=False), strava_http),
        settings,
    )
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    _logger.info("sync.worker_started", extra={"fields": {"worker_id": settings.worker_id}})
    try:
        worker.run_forever(stop)
    finally:
        strava_http.close()
        engine.dispose()
        _logger.info("sync.worker_stopped")


if __name__ == "__main__":
    main()
