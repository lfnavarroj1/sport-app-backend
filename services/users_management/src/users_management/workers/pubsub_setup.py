"""Crea en el **emulador** de Pub/Sub los recursos de mensajería de usuarios (local y CI).

Ejecución: `python -m users_management.workers.pubsub_setup`.

Tópico `sportapp.usuarios.v1`, tópico DLQ `sportapp.usuarios.proyeccion.dlq`, la
suscripción ordenada `proyector-usuarios-v1` de `usuarios-read-projector` (DLQ tras 5
entregas, backoff 10 a 600 s, CMP-001) y la suscripción de operación de la DLQ. En GCP los
crea la infraestructura como código; por eso este comando se niega a ejecutarse sin
`PUBSUB_EMULATOR_HOST`. Repetirlo es inofensivo: los recursos existentes no cambian.
"""

import contextlib
import os
from datetime import timedelta

from google.api_core.exceptions import AlreadyExists
from google.cloud import pubsub_v1

from sportapp_platform.config import ConfigurationError
from users_management.settings import (
    MAX_DELIVERY_ATTEMPTS,
    PROJECTOR_DLQ_OPS_SUBSCRIPTION,
    PROJECTOR_DLQ_TOPIC,
    PROJECTOR_SUBSCRIPTION,
    RETRY_MAX_BACKOFF_SECONDS,
    RETRY_MIN_BACKOFF_SECONDS,
    USERS_TOPIC,
)


def _require_emulator() -> None:
    if not os.environ.get("PUBSUB_EMULATOR_HOST"):
        raise ConfigurationError("Solo se permite contra el emulador: falta PUBSUB_EMULATOR_HOST")


def ensure_topic(project_id: str, topic: str) -> str:
    _require_emulator()
    client = pubsub_v1.PublisherClient()
    topic_path = str(client.topic_path(project_id, topic))
    with contextlib.suppress(AlreadyExists):
        client.create_topic(name=topic_path)
    return topic_path


def ensure_resources(project_id: str, topic: str = USERS_TOPIC) -> list[str]:
    """Tópicos y suscripciones del proyector; devuelve sus rutas en orden de creación."""
    _require_emulator()
    topics = {name: ensure_topic(project_id, name) for name in (topic, PROJECTOR_DLQ_TOPIC)}
    created = list(topics.values())
    subscriber = pubsub_v1.SubscriberClient()
    try:
        types = pubsub_v1.types
        subscriptions = [
            types.Subscription(
                name=subscriber.subscription_path(project_id, PROJECTOR_SUBSCRIPTION),
                topic=topics[topic],
                enable_message_ordering=True,
                dead_letter_policy=types.DeadLetterPolicy(
                    dead_letter_topic=topics[PROJECTOR_DLQ_TOPIC],
                    max_delivery_attempts=MAX_DELIVERY_ATTEMPTS,
                ),
                retry_policy=types.RetryPolicy(
                    minimum_backoff=timedelta(seconds=RETRY_MIN_BACKOFF_SECONDS),
                    maximum_backoff=timedelta(seconds=RETRY_MAX_BACKOFF_SECONDS),
                ),
            ),
            types.Subscription(
                name=subscriber.subscription_path(project_id, PROJECTOR_DLQ_OPS_SUBSCRIPTION),
                topic=topics[PROJECTOR_DLQ_TOPIC],
            ),
        ]
        for subscription in subscriptions:
            with contextlib.suppress(AlreadyExists):
                subscriber.create_subscription(request=subscription)
            created.append(str(subscription.name))
    finally:
        subscriber.close()
    return created


def main() -> None:
    project_id = os.environ.get("PUBSUB_PROJECT_ID", "")
    if not project_id:
        raise ConfigurationError("Configuración inválida o incompleta: PUBSUB_PROJECT_ID")
    for path in ensure_resources(project_id, os.environ.get("PUBSUB_TOPIC", USERS_TOPIC)):
        print(path)


if __name__ == "__main__":
    main()
