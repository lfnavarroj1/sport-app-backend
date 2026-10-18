"""`eventos-read-projector`: construye la base de lectura de eventos masivos (CMP-004).

Ejecución: `python -m events_management.workers.read_projector`.

- Proyecta `evento.creado.v1` y `evento.publicado.v1` por estado completo (ADR-033) y
  `usuario.inscrito.v1` (HU041).
- Consume `sportapp.eventos.v1` por la suscripción ordenada `proyector-eventos-v1`.
- Confirma (ACK) el mensaje solo después del COMMIT de `event_read` y `processed_event`.
- Un mensaje inválido o un fallo de procesamiento no se confirma:
  Pub/Sub lo reintenta con el backoff de la suscripción (10 a 600 s) y, tras 10
  intentos, lo envía a `sportapp.eventos.proyeccion.dlq`.
- Un tipo de evento que este proyector aún no proyecta se confirma sin efectos.
"""

import json
import logging
import signal
from collections.abc import Callable
from typing import Protocol

from google.cloud import pubsub_v1
from sqlalchemy.orm import sessionmaker

from events_management.application.messages import (
    InvalidEvent,
    RegistrationCreatedMessage,
    SportEventMessage,
    UnsupportedEvent,
    parse_sport_event_message,
)
from events_management.application.projections.event_projection import (
    EventsProjector,
    ProjectionOutcome,
)
from events_management.infrastructure.clock import SystemClock
from events_management.infrastructure.persistence_read.events import (
    SqlAlchemyProjectionUnitOfWork,
)
from events_management.settings import ReadProjectorSettings
from sportapp_platform.config import load_settings
from sportapp_platform.database import create_database_engine
from sportapp_platform.logging import configure_logging

_logger = logging.getLogger("sportapp.projector")


class InboundMessage(Protocol):
    @property
    def data(self) -> bytes: ...

    @property
    def message_id(self) -> str: ...

    @property
    def delivery_attempt(self) -> int | None: ...

    def ack(self) -> None: ...

    def nack(self) -> None: ...


class ProjectionMessageHandler:
    def __init__(self, project: Callable[[SportEventMessage], ProjectionOutcome]) -> None:
        self._project = project

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
                "projector.ignored_event",
                extra={"fields": {**fields, "event_type": unsupported.event_type}},
            )
            return
        except InvalidEvent as error:
            self._reject(message, {**fields, "reason": error.reason})
            return
        except ValueError:
            self._reject(message, {**fields, "reason": "invalid_json"})
            return
        fields |= {
            "event_id": str(event.message_id),
            "aggregate_id": str(
                event.registration.registration_id
                if isinstance(event, RegistrationCreatedMessage)
                else event.event.event_id
            ),
            "correlation_id": str(event.correlation_id) if event.correlation_id else None,
        }
        try:
            outcome = self._project(event)
        except Exception as error:
            _logger.warning(
                "projector.processing_failed",
                extra={"fields": {**fields, "error_type": type(error).__name__}},
            )
            message.nack()
            return
        message.ack()
        _logger.info("projector.processed", extra={"fields": {**fields, "outcome": outcome.value}})

    @staticmethod
    def _reject(message: InboundMessage, fields: dict[str, object]) -> None:
        # No se confirma: agotados los reintentos, termina en la DLQ para diagnóstico.
        _logger.warning("projector.invalid_event", extra={"fields": fields})
        message.nack()


def main() -> None:
    settings = load_settings(ReadProjectorSettings)
    configure_logging(
        service="events-management-read-projector",
        environment=settings.app_env,
        version=settings.service_version,
        level=settings.log_level,
    )
    engine = create_database_engine(settings.database_read_url, settings)
    project = EventsProjector(
        unit_of_work=lambda: SqlAlchemyProjectionUnitOfWork(sessionmaker(engine)),
        clock=SystemClock(),
    )
    subscriber = pubsub_v1.SubscriberClient()
    subscription = subscriber.subscription_path(
        settings.pubsub_project_id, settings.projector_subscription
    )
    streaming = subscriber.subscribe(
        subscription,
        callback=ProjectionMessageHandler(project),
        flow_control=pubsub_v1.types.FlowControl(max_messages=settings.consumer_max_messages),
    )
    signal.signal(signal.SIGTERM, lambda *_: streaming.cancel())
    signal.signal(signal.SIGINT, lambda *_: streaming.cancel())
    _logger.info("projector.started", extra={"fields": {"subscription": subscription}})
    try:
        streaming.result()
    except Exception:
        _logger.info("projector.stopping")
    finally:
        subscriber.close()
        engine.dispose()
        _logger.info("projector.stopped")


if __name__ == "__main__":
    main()
