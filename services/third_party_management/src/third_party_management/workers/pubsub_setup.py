"""Crea en el **emulador** de Pub/Sub el tópico de terceros de ADR-042 (local y CI).

Ejecución: `python -m third_party_management.workers.pubsub_setup`.

Solo el tópico `sportapp.terceros.v1`: la suscripción la posee el consumidor
(`training_management`). En GCP lo crea la infraestructura como código; por eso este
comando se niega a ejecutarse sin `PUBSUB_EMULATOR_HOST`.
"""

import contextlib
import os

from google.api_core.exceptions import AlreadyExists
from google.cloud import pubsub_v1

from sportapp_platform.config import ConfigurationError
from third_party_management.settings import TERCEROS_TOPIC


def ensure_resources(project_id: str) -> list[str]:
    if not os.environ.get("PUBSUB_EMULATOR_HOST"):
        raise ConfigurationError("Solo se permite contra el emulador: falta PUBSUB_EMULATOR_HOST")
    publisher = pubsub_v1.PublisherClient()
    path = str(publisher.topic_path(project_id, TERCEROS_TOPIC))
    with contextlib.suppress(AlreadyExists):
        publisher.create_topic(name=path)
    return [path]


def main() -> None:
    project_id = os.environ.get("PUBSUB_PROJECT_ID", "")
    if not project_id:
        raise ConfigurationError("Configuración inválida o incompleta: PUBSUB_PROJECT_ID")
    for path in ensure_resources(project_id):
        print(path)


if __name__ == "__main__":
    main()
