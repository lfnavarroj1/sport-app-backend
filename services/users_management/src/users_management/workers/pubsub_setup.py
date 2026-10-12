"""Crea el tópico de usuarios en el **emulador** de Pub/Sub (local y CI).

Ejecución: `python -m users_management.workers.pubsub_setup`.
En GCP los tópicos se crean con la infraestructura como código; por eso este
comando se niega a ejecutarse si `PUBSUB_EMULATOR_HOST` no está definido.
"""

import contextlib
import os

from google.api_core.exceptions import AlreadyExists
from google.cloud import pubsub_v1

from sportapp_platform.config import ConfigurationError
from users_management.settings import USERS_TOPIC


def ensure_topic(project_id: str, topic: str) -> str:
    if not os.environ.get("PUBSUB_EMULATOR_HOST"):
        raise ConfigurationError("Solo se permite contra el emulador: falta PUBSUB_EMULATOR_HOST")
    client = pubsub_v1.PublisherClient()
    topic_path = str(client.topic_path(project_id, topic))
    with contextlib.suppress(AlreadyExists):
        client.create_topic(name=topic_path)
    return topic_path


def main() -> None:
    project_id = os.environ.get("PUBSUB_PROJECT_ID", "")
    if not project_id:
        raise ConfigurationError("Configuración inválida o incompleta: PUBSUB_PROJECT_ID")
    print(ensure_topic(project_id, os.environ.get("PUBSUB_TOPIC", USERS_TOPIC)))


if __name__ == "__main__":
    main()
