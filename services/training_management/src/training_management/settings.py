"""Configuración de `training_management`, validada al iniciar."""

import socket
from pathlib import Path
from typing import Annotated

from pydantic import AnyHttpUrl, Field

from sportapp_platform.config import DatabasePoolSettings, PostgresUrl, ServiceSettings

# Mensajería de CMP-003. Reintentos y DLQ del proyector: PROPUESTO (2026-10-12),
# sujetos a medición del perfil de entrenamientos; no se copian de otro dominio sin prueba.
TRAINING_TOPIC = "sportapp.entrenamientos.v1"
PROJECTOR_SUBSCRIPTION = "proyector-entrenamientos-v1"
PROJECTOR_DLQ_TOPIC = "sportapp.entrenamientos.proyeccion.dlq"
PROJECTOR_DLQ_OPS_SUBSCRIPTION = "sportapp.entrenamientos.proyeccion.dlq.ops"
MAX_DELIVERY_ATTEMPTS = 10
RETRY_MIN_BACKOFF_SECONDS = 10
RETRY_MAX_BACKOFF_SECONDS = 600


class TrainingManagementSettings(ServiceSettings, DatabasePoolSettings):
    database_write_url: PostgresUrl
    database_read_url: PostgresUrl
    # HU010: identidad (JWKS de users-management) y capacidades del deportista (Afiliación).
    users_management_base_url: AnyHttpUrl
    affiliation_management_base_url: AnyHttpUrl
    # HU042 (ADR-037): versión vigente del consentimiento de datos de salud (ADR-034); sin ella
    # el servicio no arranca.
    health_data_consent_version: Annotated[str, Field(min_length=1, max_length=64)]
    upstream_timeout_seconds: float = Field(default=5.0, gt=0, le=30)
    # `iss` y `aud` que emite users-management (mismos valores por defecto).
    jwt_issuer: str = Field(default="sportapp-users-management", min_length=1, max_length=128)
    jwt_audience: str = Field(default="sportapp", min_length=1, max_length=128)
    # Ruta del contrato `actor-permissions.v1`; sin valor se usa `ACTOR_PERMISSIONS_PATH`
    # (imagen) o `contracts/authorization/` del repositorio.
    actor_permissions_path: Path | None = None


class OutboxPublisherSettings(ServiceSettings, DatabasePoolSettings):
    """`entrenamientos-outbox-publisher`; valores iniciales de CMP-002 sujetos a prueba."""

    database_write_url: PostgresUrl
    pubsub_project_id: Annotated[str, Field(min_length=1)]
    pubsub_topic: str = TRAINING_TOPIC
    outbox_batch_size: int = Field(default=100, ge=1, le=500)
    outbox_lease_seconds: float = Field(default=30, ge=5, le=300)
    outbox_busy_poll_seconds: float = Field(default=0.25, gt=0, le=5)
    outbox_idle_poll_max_seconds: float = Field(default=2.0, gt=0, le=30)
    worker_id: str = Field(default_factory=socket.gethostname, min_length=1, max_length=128)


class ReadProjectorSettings(ServiceSettings, DatabasePoolSettings):
    """`entrenamientos-read-projector`: consume `proyector-entrenamientos-v1`."""

    database_read_url: PostgresUrl
    pubsub_project_id: Annotated[str, Field(min_length=1)]
    projector_subscription: str = PROJECTOR_SUBSCRIPTION
    # Mensajes en proceso simultáneo por réplica; acota conexiones a PostgreSQL.
    consumer_max_messages: int = Field(default=10, ge=1, le=100)


class MeasurementRetentionSettings(ServiceSettings, DatabasePoolSettings):
    """HU036: tarea de retención de `training_measurement` (90 días, ADR-044 §7)."""

    database_write_url: PostgresUrl
    retention_batch_size: int = Field(default=5000, ge=100, le=50000)


# HU031 (ADR-042 §4): actividades importadas de Strava. Terceros publica en su tópico; este
# servicio las consume por una suscripción ordenada propia con DLQ. Reintentos y DLQ iguales a
# los del proyector (10 intentos, 10–600 s), fijados por ADR-042.
THIRD_PARTY_TOPIC = "sportapp.terceros.v1"
EXTERNAL_ACTIVITY_SUBSCRIPTION = "entrenamientos-actividades-externas-v1"
EXTERNAL_ACTIVITY_DLQ_TOPIC = "sportapp.terceros.actividades.dlq"
EXTERNAL_ACTIVITY_DLQ_OPS_SUBSCRIPTION = "sportapp.terceros.actividades.dlq.ops"


class ExternalActivityConsumerSettings(ServiceSettings, DatabasePoolSettings):
    """`entrenamientos-external-activity-consumer` (HU031): consume
    `entrenamientos-actividades-externas-v1` y escribe en la base de lectura."""

    database_read_url: PostgresUrl
    pubsub_project_id: Annotated[str, Field(min_length=1)]
    external_activity_subscription: str = EXTERNAL_ACTIVITY_SUBSCRIPTION
    # Mensajes en proceso simultáneo por réplica; acota conexiones a PostgreSQL.
    consumer_max_messages: int = Field(default=10, ge=1, le=100)
