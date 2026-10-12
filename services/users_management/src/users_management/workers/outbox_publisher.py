"""`usuarios-outbox-publisher`: publica la outbox de usuarios en Pub/Sub (CMP-001).

Ejecución: `python -m users_management.workers.outbox_publisher`.

- Reclama lotes con arrendamiento y `SKIP LOCKED`; publica sin transacción abierta.
- Usa `aggregate_id` como `ordering_key`.
- Marca `published_at` solo después de la confirmación de Pub/Sub.
- Un fallo incrementa `attempts` y reprograma con backoff exponencial y jitter.
"""

import logging
import random
import signal
import threading
from datetime import UTC
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy.orm import sessionmaker

from sportapp_platform.config import load_settings
from sportapp_platform.database import create_database_engine
from sportapp_platform.logging import configure_logging
from users_management.infrastructure.messaging.pubsub import PubSubEventPublisher
from users_management.infrastructure.persistence_write.outbox import (
    ClaimedEvent,
    SqlAlchemyOutboxStore,
)
from users_management.settings import OutboxPublisherSettings

PRODUCER = "gestion-usuarios"
MAX_RETRY_SECONDS = 600.0

_logger = logging.getLogger("sportapp.outbox")


class OutboxStore(Protocol):
    def claim_batch(
        self, worker_id: str, batch_size: int, lease_seconds: float
    ) -> list[ClaimedEvent]: ...

    def mark_published(self, event_id: UUID, worker_id: str) -> None: ...

    def mark_failed(
        self, event_id: UUID, worker_id: str, error: str, retry_in_seconds: float
    ) -> None: ...


class EventPublisher(Protocol):
    def publish(self, envelope: dict[str, Any], ordering_key: str) -> None: ...


def build_envelope(event: ClaimedEvent) -> dict[str, Any]:
    """Sobre común de CMP-001 / ADR-015."""
    return {
        "event_id": str(event.event_id),
        "event_type": event.event_type,
        "occurred_at": event.occurred_at.astimezone(UTC).isoformat(),
        "producer": PRODUCER,
        "aggregate_id": str(event.aggregate_id),
        "aggregate_version": event.aggregate_version,
        "correlation_id": str(event.correlation_id) if event.correlation_id else None,
        "causation_id": event.causation_id,
        "schema_version": event.schema_version,
        "payload": event.payload,
    }


def retry_delay_seconds(attempts: int) -> float:
    base = min(MAX_RETRY_SECONDS, 2.0 ** min(attempts, 10))
    # Jitter para dispersar reintentos; no tiene uso criptográfico.
    return base + random.uniform(0, 1)  # noqa: S311  # nosec B311


class OutboxRelay:
    def __init__(
        self,
        store: OutboxStore,
        publisher: EventPublisher,
        settings: OutboxPublisherSettings,
    ) -> None:
        self._store = store
        self._publisher = publisher
        self._settings = settings

    def run_once(self) -> int:
        """Procesa un lote; devuelve cuántos eventos reclamó."""
        batch = self._store.claim_batch(
            self._settings.worker_id,
            self._settings.outbox_batch_size,
            self._settings.outbox_lease_seconds,
        )
        for event in batch:
            self._publish(event)
        return len(batch)

    def _publish(self, event: ClaimedEvent) -> None:
        fields = {
            "event_id": str(event.event_id),
            "event_type": event.event_type,
            "aggregate_id": str(event.aggregate_id),
            "aggregate_version": event.aggregate_version,
            "attempt": event.attempts + 1,
            # El worker no atiende solicitudes: la correlación proviene del evento.
            "correlation_id": str(event.correlation_id) if event.correlation_id else None,
        }
        try:
            self._publisher.publish(build_envelope(event), ordering_key=str(event.aggregate_id))
        except Exception as error:
            delay = retry_delay_seconds(event.attempts + 1)
            self._store.mark_failed(
                event.event_id, self._settings.worker_id, type(error).__name__, delay
            )
            _logger.warning(
                "outbox.publish_failed",
                extra={"fields": {**fields, "error_type": type(error).__name__}},
            )
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
        service="users-management-outbox-publisher",
        environment=settings.app_env,
        version=settings.service_version,
        level=settings.log_level,
    )
    engine = create_database_engine(settings.database_write_url, settings)
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
