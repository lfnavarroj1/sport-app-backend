"""Consumidor del alta gratuita (HU001, ADR-024).

Ejecución: `python -m affiliation_management.workers.free_plan_consumer`.

- Consume `usuario.registrado.v1` por la suscripción `sportapp.afiliacion.alta-gratuita.v1`.
- Confirma (ACK) el mensaje solo después del COMMIT de inbox, suscripción y outbox.
- Un mensaje inválido o un fallo de procesamiento no se confirma: Pub/Sub lo
  reintenta con el backoff de la suscripción y, tras 10 intentos, lo envía a la DLQ.
- Otro tipo de evento del tópico (p. ej. `perfil.*`) se confirma sin efectos: la
  suscripción ya filtra por `event_type` y esto es la defensa adicional de R-19 (ADR-034).
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

from affiliation_management.application.commands.provision_free_subscription import (
    ProvisionFreeSubscription,
    ProvisioningOutcome,
)
from affiliation_management.application.messages import (
    InvalidEvent,
    RegisteredUser,
    UnsupportedEvent,
    parse_user_registered,
)
from affiliation_management.infrastructure.clock import SystemClock
from affiliation_management.infrastructure.persistence.unit_of_work import (
    SqlAlchemyProvisioningUnitOfWork,
)
from affiliation_management.settings import FreePlanConsumerSettings
from sportapp_platform.config import load_settings
from sportapp_platform.database import create_database_engine
from sportapp_platform.logging import configure_logging

_logger = logging.getLogger("sportapp.consumer")


class InboundMessage(Protocol):
    @property
    def data(self) -> bytes: ...

    @property
    def message_id(self) -> str: ...

    @property
    def delivery_attempt(self) -> int | None: ...

    def ack(self) -> None: ...

    def nack(self) -> None: ...


class FreePlanMessageHandler:
    def __init__(self, provision: Callable[[RegisteredUser], ProvisioningOutcome]) -> None:
        self._provision = provision

    def __call__(self, message: InboundMessage) -> None:
        fields: dict[str, object] = {
            "message_id": message.message_id,
            "delivery_attempt": message.delivery_attempt,
        }
        try:
            event = parse_user_registered(json.loads(message.data))
        except UnsupportedEvent as unsupported:
            message.ack()
            _logger.info(
                "consumer.ignored_event",
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
            "correlation_id": str(event.correlation_id) if event.correlation_id else None,
        }
        try:
            outcome = self._provision(event)
        except Exception as error:
            _logger.warning(
                "consumer.processing_failed",
                extra={"fields": {**fields, "error_type": type(error).__name__}},
            )
            message.nack()
            return
        message.ack()
        _logger.info("consumer.processed", extra={"fields": {**fields, "outcome": outcome.value}})

    @staticmethod
    def _reject(message: InboundMessage, fields: dict[str, object]) -> None:
        # No se confirma: agotados los reintentos, termina en la DLQ para diagnóstico.
        _logger.warning("consumer.invalid_event", extra={"fields": fields})
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
            _logger.info("consumer.stopping")
            return 0
        _logger.error(
            "consumer.subscription_failed",
            extra={"fields": {"error_type": type(error).__name__}},
        )
        return 1
    if stop_requested.is_set():
        return 0
    _logger.error("consumer.subscription_failed", extra={"fields": {"error_type": None}})
    return 1


def main() -> None:
    settings = load_settings(FreePlanConsumerSettings)
    configure_logging(
        service="affiliation-management-free-plan-consumer",
        environment=settings.app_env,
        version=settings.service_version,
        level=settings.log_level,
    )
    engine = create_database_engine(settings.database_url, settings)
    provision = ProvisionFreeSubscription(
        unit_of_work=lambda: SqlAlchemyProvisioningUnitOfWork(sessionmaker(engine)),
        clock=SystemClock(),
    )
    subscriber = pubsub_v1.SubscriberClient()
    subscription = subscriber.subscription_path(
        settings.pubsub_project_id, settings.free_plan_subscription
    )
    streaming = subscriber.subscribe(
        subscription,
        callback=FreePlanMessageHandler(provision),
        flow_control=pubsub_v1.types.FlowControl(max_messages=settings.consumer_max_messages),
    )
    stop_requested = threading.Event()

    def stop(*_: object) -> None:
        stop_requested.set()
        streaming.cancel()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    _logger.info("consumer.started", extra={"fields": {"subscription": subscription}})
    try:
        exit_code = await_subscription(streaming, stop_requested)
    finally:
        subscriber.close()
        engine.dispose()
        _logger.info("consumer.stopped")
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
