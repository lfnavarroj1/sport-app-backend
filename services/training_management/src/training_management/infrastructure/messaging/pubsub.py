"""Publicación en Google Cloud Pub/Sub con orden por agregado (`plan_id`).

Con `PUBSUB_EMULATOR_HOST` definido, el cliente usa el emulador local.
"""

import json
from typing import Any

from google.cloud import pubsub_v1

PUBLISH_TIMEOUT_SECONDS = 30.0


class PubSubEventPublisher:
    def __init__(self, project_id: str, topic: str) -> None:
        self._client = pubsub_v1.PublisherClient(
            publisher_options=pubsub_v1.types.PublisherOptions(enable_message_ordering=True)
        )
        self._topic_path = self._client.topic_path(project_id, topic)

    def publish(self, envelope: dict[str, Any], ordering_key: str) -> None:
        """Retorna solo cuando Pub/Sub confirma la publicación."""
        data = json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        future = self._client.publish(
            self._topic_path,
            data,
            ordering_key=ordering_key,
            event_id=envelope["event_id"],
            event_type=envelope["event_type"],
            schema_version=str(envelope["schema_version"]),
            correlation_id=envelope.get("correlation_id") or "",
        )
        try:
            future.result(timeout=PUBLISH_TIMEOUT_SECONDS)
        except Exception:
            # Con orden habilitado, un fallo pausa la clave hasta reanudarla explícitamente.
            self._client.resume_publish(self._topic_path, ordering_key)
            raise

    def close(self) -> None:
        self._client.stop()
