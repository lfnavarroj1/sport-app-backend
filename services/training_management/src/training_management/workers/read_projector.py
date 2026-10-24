"""`entrenamientos-read-projector`: construye la base de lectura de entrenamientos (CMP-003).

Ejecución: `python -m training_management.workers.read_projector`.

- Consume `sportapp.entrenamientos.v1` por la suscripción ordenada `proyector-entrenamientos-v1`.
- Confirma (ACK) el mensaje solo después del COMMIT de la vista y `processed_event`.
- Un mensaje inválido, un hueco de versión, un cálculo ya proyectado con otros datos
  (HU034) o un fallo de procesamiento no se confirma: Pub/Sub lo reintenta con el
  backoff de la suscripción y, tras 10 intentos, lo envía a
  `sportapp.entrenamientos.proyeccion.dlq`.
- Un tipo de evento que este proyector aún no proyecta se confirma sin efectos.
"""

import json
import logging
import signal
import sys
import threading
from collections.abc import Callable
from typing import Protocol

from google.cloud import pubsub_v1
from sqlalchemy.orm import sessionmaker

from sportapp_platform.config import load_settings
from sportapp_platform.database import create_database_engine
from sportapp_platform.logging import configure_logging
from training_management.application.messages import (
    AuthorizationChangedEvent,
    InvalidEvent,
    MetricCalculatedEvent,
    NutritionPlanCreatedEvent,
    PlanCreatedEvent,
    RouteChangedEvent,
    TemplateChangedEvent,
    TrainingEvent,
    UnsupportedEvent,
    parse_training_event,
)
from training_management.application.projections.plan_projection import ProjectionOutcome
from training_management.application.projections.snapshot_projection import TrainingProjector
from training_management.infrastructure.clock import SystemClock
from training_management.infrastructure.persistence_read.plans import (
    SqlAlchemyProjectionUnitOfWork,
)
from training_management.settings import ReadProjectorSettings

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


def _aggregate_id(event: TrainingEvent) -> object:
    if isinstance(event, PlanCreatedEvent | NutritionPlanCreatedEvent):
        return event.plan.plan_id
    if isinstance(event, AuthorizationChangedEvent):
        return event.aggregate_id
    if isinstance(event, MetricCalculatedEvent):
        return event.calculation.calculation_id
    if isinstance(event, RouteChangedEvent):
        return event.route.route_id
    if isinstance(event, TemplateChangedEvent):
        return event.template.template_id
    return event.session.session_id


class ProjectionMessageHandler:
    def __init__(self, project: Callable[[TrainingEvent], ProjectionOutcome]) -> None:
        self._project = project

    def __call__(self, message: InboundMessage) -> None:
        fields: dict[str, object] = {
            "message_id": message.message_id,
            "delivery_attempt": message.delivery_attempt,
        }
        try:
            event = parse_training_event(json.loads(message.data))
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
            "event_id": str(event.event_id),
            "aggregate_id": str(_aggregate_id(event)),
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


class StreamingPull(Protocol):
    def result(self) -> object: ...


def await_subscription(streaming: StreamingPull, stop_requested: threading.Event) -> int:
    """Espera la suscripción y devuelve el código de salida del proceso.

    `0` si se detuvo por una señal. `1` si la suscripción terminó por sí sola (p. ej.
    `NotFound` porque el emulador perdió sus recursos): se registra en `ERROR` para que
    la causa sea visible y Kubernetes lo trate como fallo, en lugar de ocultarlo.
    """
    try:
        streaming.result()
    except Exception as error:
        if stop_requested.is_set():
            _logger.info("projector.stopping")
            return 0
        _logger.error(
            "projector.subscription_failed",
            extra={"fields": {"error_type": type(error).__name__}},
        )
        return 1
    if stop_requested.is_set():
        return 0
    _logger.error("projector.subscription_failed", extra={"fields": {"error_type": None}})
    return 1


def main() -> None:
    settings = load_settings(ReadProjectorSettings)
    configure_logging(
        service="training-management-read-projector",
        environment=settings.app_env,
        version=settings.service_version,
        level=settings.log_level,
    )
    engine = create_database_engine(settings.database_read_url, settings)
    project = TrainingProjector(
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
    stop_requested = threading.Event()

    def stop(*_: object) -> None:
        stop_requested.set()
        streaming.cancel()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    _logger.info("projector.started", extra={"fields": {"subscription": subscription}})
    try:
        exit_code = await_subscription(streaming, stop_requested)
    finally:
        subscriber.close()
        engine.dispose()
        _logger.info("projector.stopped")
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
