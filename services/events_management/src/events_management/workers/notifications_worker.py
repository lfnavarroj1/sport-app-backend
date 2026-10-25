"""`eventos-notificaciones-worker`: alertas `EVENT_PUBLISHED_MATCH` (HU039, ADR-045 §8).

Ejecución: `python -m events_management.workers.notifications_worker`.

- Consume `sportapp.eventos.v1` por su suscripción propia `eventos-notificaciones-v1`
  (filtrada a `evento.publicado.v1`, DLQ `sportapp.eventos.notificaciones.dlq`).
- Por cada evento publicado busca destinatarios en Usuarios (`POST /v1/users/athletes/search`)
  e inserta notificaciones en lotes de 100 con su outbox (`notificacion.solicitada.v1`).
- Confirma (ACK) el mensaje solo cuando todos los lotes quedaron confirmados. Si Usuarios o
  la base fallan no se confirma: Pub/Sub lo reintenta (10 a 600 s) y, tras 10 intentos, lo
  envía a la DLQ; los lotes ya escritos se deduplican por `dedupe_key` (CA3, CA4).
- Un mensaje inválido no se confirma (termina en la DLQ); otro tipo de evento se confirma
  sin efectos. La publicación del evento nunca espera a este worker.
"""

import json
import logging
import signal
from collections.abc import Callable
from typing import Protocol

import httpx
from google.cloud import pubsub_v1
from sqlalchemy.orm import sessionmaker

from events_management.application.commands.generate_notifications import (
    GenerateEventMatchNotifications,
    GenerationResult,
)
from events_management.application.messages import (
    EventPublishedMessage,
    InvalidEvent,
    UnsupportedEvent,
    parse_sport_event_message,
)
from events_management.domain.notifications.disciplines import load_aliases
from events_management.infrastructure.clock import SystemClock
from events_management.infrastructure.integrations.users import UsersAthleteDirectory
from events_management.infrastructure.persistence_write.notifications import (
    SqlAlchemyNotificationUnitOfWork,
)
from events_management.settings import NotificationsWorkerSettings
from sportapp_platform.config import load_settings
from sportapp_platform.correlation import correlation_id_var
from sportapp_platform.database import create_database_engine
from sportapp_platform.logging import configure_logging

_logger = logging.getLogger("sportapp.notifications")


class InboundMessage(Protocol):
    @property
    def data(self) -> bytes: ...

    @property
    def message_id(self) -> str: ...

    @property
    def delivery_attempt(self) -> int | None: ...

    def ack(self) -> None: ...

    def nack(self) -> None: ...


class NotificationMessageHandler:
    def __init__(self, generate: Callable[..., GenerationResult]) -> None:
        self._generate = generate

    def __call__(self, message: InboundMessage) -> None:
        fields: dict[str, object] = {
            "message_id": message.message_id,
            "delivery_attempt": message.delivery_attempt,
        }
        try:
            event = parse_sport_event_message(json.loads(message.data))
        except UnsupportedEvent as unsupported:
            message.ack()
            _logger.info(
                "notifications.ignored_event",
                extra={"fields": {**fields, "event_type": unsupported.event_type}},
            )
            return
        except InvalidEvent as error:
            self._reject(message, {**fields, "reason": error.reason})
            return
        except ValueError:
            self._reject(message, {**fields, "reason": "invalid_json"})
            return
        if not isinstance(event, EventPublishedMessage):
            message.ack()
            _logger.info("notifications.ignored_event", extra={"fields": fields})
            return
        fields |= {
            "event_id": str(event.message_id),
            "aggregate_id": str(event.event.event_id),
            "correlation_id": str(event.correlation_id) if event.correlation_id else None,
        }
        token = correlation_id_var.set(str(event.correlation_id) if event.correlation_id else None)
        try:
            result = self._generate(
                event.event, message_id=event.message_id, correlation_id=event.correlation_id
            )
        except Exception as error:
            _logger.warning(
                "notifications.processing_failed",
                extra={"fields": {**fields, "error_type": type(error).__name__}},
            )
            message.nack()
            return
        finally:
            correlation_id_var.reset(token)
        message.ack()
        _logger.info(
            "notifications.generated",
            extra={
                "fields": {
                    **fields,
                    "generated": result.generated,
                    "duplicates": result.duplicates,
                    "disabled": result.disabled,
                }
            },
        )

    @staticmethod
    def _reject(message: InboundMessage, fields: dict[str, object]) -> None:
        _logger.warning("notifications.invalid_event", extra={"fields": fields})
        message.nack()


def main() -> None:
    settings = load_settings(NotificationsWorkerSettings)
    configure_logging(
        service="events-management-notifications-worker",
        environment=settings.app_env,
        version=settings.service_version,
        level=settings.log_level,
    )
    engine = create_database_engine(settings.database_write_url, settings)
    users_http = httpx.Client(
        base_url=str(settings.users_management_base_url),
        timeout=settings.upstream_timeout_seconds,
    )
    generate = GenerateEventMatchNotifications(
        unit_of_work=lambda: SqlAlchemyNotificationUnitOfWork(sessionmaker(engine)),
        directory=UsersAthleteDirectory(users_http),
        aliases=load_aliases(),
        clock=SystemClock(),
    )
    subscriber = pubsub_v1.SubscriberClient()
    subscription = subscriber.subscription_path(
        settings.pubsub_project_id, settings.notifications_subscription
    )
    streaming = subscriber.subscribe(
        subscription,
        callback=NotificationMessageHandler(generate),
        flow_control=pubsub_v1.types.FlowControl(max_messages=settings.consumer_max_messages),
    )
    signal.signal(signal.SIGTERM, lambda *_: streaming.cancel())
    signal.signal(signal.SIGINT, lambda *_: streaming.cancel())
    _logger.info("notifications.started", extra={"fields": {"subscription": subscription}})
    try:
        streaming.result()
    except Exception:
        _logger.info("notifications.stopping")
    finally:
        subscriber.close()
        users_http.close()
        engine.dispose()
        _logger.info("notifications.stopped")


if __name__ == "__main__":
    main()
