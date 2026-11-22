"""`entrenamientos-external-activity-consumer`: actividades importadas de Strava (HU031, ADR-042).

Ejecución: `python -m training_management.workers.external_activity_consumer`.
Solo en el emulador (local y CI): `... external_activity_consumer setup` crea los recursos de
mensajería (`ensure_resources`); en GCP los crea la infraestructura como código.

- Consume `sportapp.terceros.v1` por la suscripción ordenada
  `entrenamientos-actividades-externas-v1` (`ordering_key = user_id`).
- Escribe `training_session_history` (`source = STRAVA`) y `processed_event` en una sola
  transacción de la base de lectura y confirma (ACK) el mensaje solo después del COMMIT.
- Un mensaje inválido o un fallo de procesamiento no se confirma: Pub/Sub lo reintenta con
  el backoff de la suscripción (10 a 600 s) y, tras 10 intentos, lo envía a
  `sportapp.terceros.actividades.dlq`.
- Otro tipo de evento del tópico se confirma sin efectos.
- Nunca crea una sesión activa ni publica eventos. Los registros llevan identificadores y
  códigos fijos, nunca el nombre de la actividad (ni ningún otro valor del payload).
"""

import contextlib
import json
import logging
import os
import signal
import sys
import threading
from collections.abc import Callable, Sequence
from datetime import timedelta
from typing import Protocol

from google.api_core.exceptions import AlreadyExists
from google.cloud import pubsub_v1
from sqlalchemy.orm import sessionmaker

from sportapp_platform.config import ConfigurationError, load_settings
from sportapp_platform.database import create_database_engine
from sportapp_platform.logging import configure_logging
from training_management.application.external_activity import (
    ExternalActivityImported,
    parse_external_activity_event,
)
from training_management.application.messages import InvalidEvent, UnsupportedEvent
from training_management.application.projections.external_activity_projection import (
    ProjectExternalActivity,
)
from training_management.application.projections.plan_projection import ProjectionOutcome
from training_management.infrastructure.clock import SystemClock
from training_management.infrastructure.persistence_read.external_activities import (
    SqlAlchemyExternalActivityUnitOfWork,
)
from training_management.settings import (
    EXTERNAL_ACTIVITY_DLQ_OPS_SUBSCRIPTION,
    EXTERNAL_ACTIVITY_DLQ_TOPIC,
    EXTERNAL_ACTIVITY_SUBSCRIPTION,
    MAX_DELIVERY_ATTEMPTS,
    RETRY_MAX_BACKOFF_SECONDS,
    RETRY_MIN_BACKOFF_SECONDS,
    THIRD_PARTY_TOPIC,
    ExternalActivityConsumerSettings,
)

_logger = logging.getLogger("sportapp.external_activity_consumer")


class InboundMessage(Protocol):
    @property
    def data(self) -> bytes: ...

    @property
    def message_id(self) -> str: ...

    @property
    def delivery_attempt(self) -> int | None: ...

    def ack(self) -> None: ...

    def nack(self) -> None: ...


class ExternalActivityMessageHandler:
    def __init__(self, project: Callable[[ExternalActivityImported], ProjectionOutcome]) -> None:
        self._project = project

    def __call__(self, message: InboundMessage) -> None:
        fields: dict[str, object] = {
            "message_id": message.message_id,
            "delivery_attempt": message.delivery_attempt,
        }
        try:
            event = parse_external_activity_event(json.loads(message.data))
        except UnsupportedEvent as unsupported:
            message.ack()
            _logger.info(
                "external_activity.ignored_event",
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
            "activity_id": str(event.activity.activity_id),
            "aggregate_version": event.activity.version,
            "correlation_id": str(event.correlation_id) if event.correlation_id else None,
            "causation_id": str(event.causation_id),
        }
        try:
            outcome = self._project(event)
        except Exception as error:
            _logger.warning(
                "external_activity.processing_failed",
                extra={"fields": {**fields, "error_type": type(error).__name__}},
            )
            message.nack()
            return
        message.ack()
        _logger.info(
            "external_activity.processed", extra={"fields": {**fields, "outcome": outcome.value}}
        )

    @staticmethod
    def _reject(message: InboundMessage, fields: dict[str, object]) -> None:
        # No se confirma: agotados los reintentos, termina en la DLQ para diagnóstico.
        _logger.warning("external_activity.invalid_event", extra={"fields": fields})
        message.nack()


class StreamingPull(Protocol):
    def result(self) -> object: ...


def await_subscription(streaming: StreamingPull, stop_requested: threading.Event) -> int:
    """Espera la suscripción y devuelve el código de salida del proceso.

    `0` si se detuvo por una señal. `1` si la suscripción terminó por sí sola (p. ej.
    `NotFound` porque el emulador perdió sus recursos): se registra en `ERROR` para que la
    causa sea visible y Kubernetes lo trate como fallo.
    """
    try:
        streaming.result()
    except Exception as error:
        if stop_requested.is_set():
            _logger.info("external_activity.stopping")
            return 0
        _logger.error(
            "external_activity.subscription_failed",
            extra={"fields": {"error_type": type(error).__name__}},
        )
        return 1
    if stop_requested.is_set():
        return 0
    _logger.error("external_activity.subscription_failed", extra={"fields": {"error_type": None}})
    return 1


def ensure_resources(project_id: str) -> list[str]:
    """Crea en el **emulador** el tópico de Terceros, la suscripción ordenada con DLQ y la
    suscripción de operación de la DLQ. Se niega a ejecutarse sin `PUBSUB_EMULATOR_HOST`."""
    if not os.environ.get("PUBSUB_EMULATOR_HOST"):
        raise ConfigurationError("Solo se permite contra el emulador: falta PUBSUB_EMULATOR_HOST")
    publisher = pubsub_v1.PublisherClient()
    subscriber = pubsub_v1.SubscriberClient()
    created: list[str] = []
    try:
        topics = {
            name: str(publisher.topic_path(project_id, name))
            for name in (THIRD_PARTY_TOPIC, EXTERNAL_ACTIVITY_DLQ_TOPIC)
        }
        for path in topics.values():
            with contextlib.suppress(AlreadyExists):
                publisher.create_topic(name=path)
            created.append(path)
        types = pubsub_v1.types
        subscriptions = [
            types.Subscription(
                name=subscriber.subscription_path(project_id, EXTERNAL_ACTIVITY_SUBSCRIPTION),
                topic=topics[THIRD_PARTY_TOPIC],
                enable_message_ordering=True,
                dead_letter_policy=types.DeadLetterPolicy(
                    dead_letter_topic=topics[EXTERNAL_ACTIVITY_DLQ_TOPIC],
                    max_delivery_attempts=MAX_DELIVERY_ATTEMPTS,
                ),
                retry_policy=types.RetryPolicy(
                    minimum_backoff=timedelta(seconds=RETRY_MIN_BACKOFF_SECONDS),
                    maximum_backoff=timedelta(seconds=RETRY_MAX_BACKOFF_SECONDS),
                ),
            ),
            types.Subscription(
                name=subscriber.subscription_path(
                    project_id, EXTERNAL_ACTIVITY_DLQ_OPS_SUBSCRIPTION
                ),
                topic=topics[EXTERNAL_ACTIVITY_DLQ_TOPIC],
            ),
        ]
        for subscription in subscriptions:
            with contextlib.suppress(AlreadyExists):
                subscriber.create_subscription(request=subscription)
            created.append(str(subscription.name))
    finally:
        subscriber.close()
    return created


def _setup() -> None:
    project_id = os.environ.get("PUBSUB_PROJECT_ID", "")
    if not project_id:
        raise ConfigurationError("Configuración inválida o incompleta: PUBSUB_PROJECT_ID")
    for path in ensure_resources(project_id):
        print(path)


def _consume() -> None:
    settings = load_settings(ExternalActivityConsumerSettings)
    configure_logging(
        service="training-management-external-activity-consumer",
        environment=settings.app_env,
        version=settings.service_version,
        level=settings.log_level,
    )
    engine = create_database_engine(settings.database_read_url, settings)
    project = ProjectExternalActivity(
        unit_of_work=lambda: SqlAlchemyExternalActivityUnitOfWork(sessionmaker(engine)),
        clock=SystemClock(),
    )
    subscriber = pubsub_v1.SubscriberClient()
    subscription = subscriber.subscription_path(
        settings.pubsub_project_id, settings.external_activity_subscription
    )
    streaming = subscriber.subscribe(
        subscription,
        callback=ExternalActivityMessageHandler(project),
        flow_control=pubsub_v1.types.FlowControl(max_messages=settings.consumer_max_messages),
    )
    stop_requested = threading.Event()

    def stop(*_: object) -> None:
        stop_requested.set()
        streaming.cancel()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    _logger.info("external_activity.started", extra={"fields": {"subscription": subscription}})
    try:
        exit_code = await_subscription(streaming, stop_requested)
    finally:
        subscriber.close()
        engine.dispose()
        _logger.info("external_activity.stopped")
    sys.exit(exit_code)


def main(argv: Sequence[str] | None = None) -> None:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments == ["setup"]:
        _setup()
        return
    if arguments:
        raise ConfigurationError("Uso: external_activity_consumer [setup]")
    _consume()


if __name__ == "__main__":
    main()
