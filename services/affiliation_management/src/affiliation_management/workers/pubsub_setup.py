"""Crea en el **emulador** de Pub/Sub los recursos de mensajería de ADR-024 (local y CI).

Ejecución: `python -m affiliation_management.workers.pubsub_setup`.

En GCP los crea la infraestructura como código (`T-HU001-PLAT`); por eso este
comando se niega a ejecutarse si `PUBSUB_EMULATOR_HOST` no está definido.
"""

import contextlib
import os
from datetime import timedelta

from google.api_core.exceptions import AlreadyExists
from google.cloud import pubsub_v1

from affiliation_management.settings import (
    AFFILIATION_TOPIC,
    FREE_PLAN_DLQ_OPS_SUBSCRIPTION,
    FREE_PLAN_DLQ_TOPIC,
    FREE_PLAN_SUBSCRIPTION,
    MAX_DELIVERY_ATTEMPTS,
    RETRY_MAX_BACKOFF_SECONDS,
    RETRY_MIN_BACKOFF_SECONDS,
    USERS_TOPIC,
)
from sportapp_platform.config import ConfigurationError


def ensure_resources(project_id: str) -> list[str]:
    if not os.environ.get("PUBSUB_EMULATOR_HOST"):
        raise ConfigurationError("Solo se permite contra el emulador: falta PUBSUB_EMULATOR_HOST")
    publisher = pubsub_v1.PublisherClient()
    subscriber = pubsub_v1.SubscriberClient()
    created: list[str] = []
    try:
        topics = {
            name: str(publisher.topic_path(project_id, name))
            for name in (USERS_TOPIC, AFFILIATION_TOPIC, FREE_PLAN_DLQ_TOPIC)
        }
        for path in topics.values():
            with contextlib.suppress(AlreadyExists):
                publisher.create_topic(name=path)
            created.append(path)
        types = pubsub_v1.types
        subscriptions = [
            types.Subscription(
                name=subscriber.subscription_path(project_id, FREE_PLAN_SUBSCRIPTION),
                topic=topics[USERS_TOPIC],
                enable_message_ordering=True,
                dead_letter_policy=types.DeadLetterPolicy(
                    dead_letter_topic=topics[FREE_PLAN_DLQ_TOPIC],
                    max_delivery_attempts=MAX_DELIVERY_ATTEMPTS,
                ),
                retry_policy=types.RetryPolicy(
                    minimum_backoff=timedelta(seconds=RETRY_MIN_BACKOFF_SECONDS),
                    maximum_backoff=timedelta(seconds=RETRY_MAX_BACKOFF_SECONDS),
                ),
            ),
            types.Subscription(
                name=subscriber.subscription_path(project_id, FREE_PLAN_DLQ_OPS_SUBSCRIPTION),
                topic=topics[FREE_PLAN_DLQ_TOPIC],
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
    for path in ensure_resources(project_id):
        print(path)


if __name__ == "__main__":
    main()
