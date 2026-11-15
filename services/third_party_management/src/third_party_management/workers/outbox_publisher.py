"""`terceros-outbox-publisher`: publica la outbox de Gestión de terceros (HU031, ADR-042).

Ejecución: `python -m third_party_management.workers.outbox_publisher`.

Reglas (CMP-002, mismas que los demás dominios):

- Reclama lotes con arrendamiento y `SKIP LOCKED`; publica sin transacción abierta.
- Publica en `sportapp.terceros.v1` con `ordering_key = user_id` (ADR-042 §4), tomado del
  payload de la fila (la outbox conserva las columnas de Servicios complementarios).
- Marca `published_at` solo después de la confirmación de Pub/Sub.
- Un fallo incrementa `attempts` y reprograma con espera progresiva de 10 a 600 s y
  jitter. El evento sigue pendiente hasta confirmarse; desde el décimo fallo se emite la
  alerta `outbox.publish_alert`.
- Los eventos publicados se conservan siete días: la purga corre como máximo una vez por
  hora y nunca toca pendientes.
"""

import logging
import random
import signal
import threading
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy.orm import sessionmaker

from sportapp_platform.config import load_settings
from sportapp_platform.database import create_database_engine
from sportapp_platform.logging import configure_logging
from third_party_management.domain.instants import utc_instant
from third_party_management.infrastructure.clock import SystemClock
from third_party_management.infrastructure.messaging.pubsub import PubSubEventPublisher
from third_party_management.infrastructure.persistence.outbox import (
    ClaimedEvent,
    SqlAlchemyOutboxStore,
)
from third_party_management.settings import (
    ALERT_FROM_ATTEMPT,
    PUBLISHED_RETENTION,
    PURGE_INTERVAL,
    RETRY_MAX_SECONDS,
    RETRY_MIN_SECONDS,
    OutboxPublisherSettings,
)

PRODUCER = "third-party-management"
# Dispersión de ±10 % alrededor de la espera base para no sincronizar reintentos.
JITTER_RATIO = 0.1

_logger = logging.getLogger("sportapp.outbox")


class OutboxStore(Protocol):
    def claim_batch(
        self, worker_id: str, batch_size: int, lease_seconds: float
    ) -> list[ClaimedEvent]: ...

    def mark_published(self, event_id: UUID, worker_id: str) -> None: ...

    def mark_failed(
        self, event_id: UUID, worker_id: str, error: str, retry_in_seconds: float
    ) -> None: ...

    def purge_published(self, older_than: datetime) -> int: ...


class EventPublisher(Protocol):
    def publish(self, envelope: dict[str, Any], ordering_key: str) -> None: ...


class Clock(Protocol):
    def now(self) -> datetime: ...


def build_envelope(event: ClaimedEvent) -> dict[str, Any]:
    """Sobre de `actividad.importada.v1` con los campos mínimos de los lineamientos."""
    return {
        "event_id": str(event.event_id),
        "event_type": event.event_type,
        "occurred_at": utc_instant(event.occurred_at),
        "producer": PRODUCER,
        "aggregate_type": event.aggregate_type,
        "aggregate_id": str(event.aggregate_id),
        "aggregate_version": event.aggregate_version,
        "correlation_id": str(event.correlation_id) if event.correlation_id else None,
        "causation_id": event.causation_id,
        "schema_version": event.schema_version,
        "payload": event.payload,
    }


def ordering_key(event: ClaimedEvent) -> str:
    """`user_id` del deportista: todas sus actividades se entregan en orden."""
    return str(event.payload.get("user_id") or event.aggregate_id)


def retry_delay_seconds(attempts: int) -> float:
    """Espera tras el fallo número `attempts`: 10·2^(n-1) s con tope de 600 s, ±10 % de
    jitter y siempre dentro del rango de 10 a 600 s."""
    base = min(RETRY_MAX_SECONDS, RETRY_MIN_SECONDS * 2.0 ** min(max(attempts, 1) - 1, 10))
    # Jitter para dispersar reintentos; no tiene uso criptográfico.
    jitter = random.uniform(-JITTER_RATIO, JITTER_RATIO) * base  # noqa: S311  # nosec B311
    return min(RETRY_MAX_SECONDS, max(RETRY_MIN_SECONDS, base + jitter))


class OutboxRelay:
    def __init__(
        self,
        store: OutboxStore,
        publisher: EventPublisher,
        settings: OutboxPublisherSettings,
        clock: Clock | None = None,
    ) -> None:
        self._store = store
        self._publisher = publisher
        self._settings = settings
        self._clock = clock or SystemClock()
        self._last_purge: datetime | None = None

    def run_once(self) -> int:
        """Purga si corresponde y procesa un lote; devuelve cuántos eventos reclamó."""
        self._purge_if_due()
        batch = self._store.claim_batch(
            self._settings.worker_id,
            self._settings.outbox_batch_size,
            self._settings.outbox_lease_seconds,
        )
        for event in batch:
            self._publish(event)
        return len(batch)

    def _purge_if_due(self) -> None:
        now = self._clock.now()
        if self._last_purge is not None and now - self._last_purge < PURGE_INTERVAL:
            return
        # Se registra antes de intentarlo: un fallo no repite la purga en cada ciclo.
        self._last_purge = now
        try:
            purged = self._store.purge_published(now - PUBLISHED_RETENTION)
        except Exception as error:
            _logger.warning(
                "outbox.purge_failed", extra={"fields": {"error_type": type(error).__name__}}
            )
            return
        _logger.info("outbox.purged", extra={"fields": {"purged": purged}})

    def _publish(self, event: ClaimedEvent) -> None:
        attempt = event.attempts + 1
        fields = {
            "event_id": str(event.event_id),
            "event_type": event.event_type,
            "aggregate_id": str(event.aggregate_id),
            "aggregate_version": event.aggregate_version,
            "attempt": attempt,
            "correlation_id": str(event.correlation_id) if event.correlation_id else None,
        }
        try:
            self._publisher.publish(build_envelope(event), ordering_key=ordering_key(event))
        except Exception as error:
            delay = retry_delay_seconds(attempt)
            self._store.mark_failed(
                event.event_id, self._settings.worker_id, type(error).__name__, delay
            )
            failure = {**fields, "error_type": type(error).__name__, "retry_in_seconds": delay}
            if attempt >= ALERT_FROM_ATTEMPT:
                _logger.error(
                    "outbox.publish_alert",
                    extra={"fields": {**failure, "attempts": attempt}},
                )
            else:
                _logger.warning("outbox.publish_failed", extra={"fields": failure})
            return
        self._store.mark_published(event.event_id, self._settings.worker_id)
        _logger.info("outbox.published", extra={"fields": fields})

    def run_forever(self, stop: threading.Event) -> None:
        idle_wait = self._settings.outbox_busy_poll_seconds
        while not stop.is_set():
            try:
                claimed = self.run_once()
            except Exception:
                _logger.exception("outbox.cycle_failed")
                claimed = 0
            if claimed >= self._settings.outbox_batch_size:
                wait = self._settings.outbox_busy_poll_seconds
                idle_wait = wait
            else:
                wait = idle_wait
                idle_wait = min(idle_wait * 2, self._settings.outbox_idle_poll_max_seconds)
            stop.wait(wait)


def main() -> None:
    settings = load_settings(OutboxPublisherSettings)
    configure_logging(
        service="third-party-management-outbox-publisher",
        environment=settings.app_env,
        version=settings.service_version,
        level=settings.log_level,
    )
    engine = create_database_engine(settings.database_url, settings)
    publisher = PubSubEventPublisher(settings.pubsub_project_id, settings.pubsub_topic)
    relay = OutboxRelay(SqlAlchemyOutboxStore(sessionmaker(engine)), publisher, settings)
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    _logger.info("outbox.started", extra={"fields": {"worker_id": settings.worker_id}})
    try:
        relay.run_forever(stop)
    finally:
        publisher.close()
        engine.dispose()
        _logger.info("outbox.stopped")


if __name__ == "__main__":
    main()
