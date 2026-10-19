"""Configuración de `events_management`, validada al iniciar."""

import socket
from pathlib import Path
from typing import Annotated

from pydantic import AnyHttpUrl, Field

from sportapp_platform.config import DatabasePoolSettings, PostgresUrl, ServiceSettings

# Mensajería de CMP-004 aprobada por ADR-028 para `evento.creado.v1`: suscripción del
# proyector con DLQ, 10 intentos y espera de 10 a 600 segundos.
EVENTS_TOPIC = "sportapp.eventos.v1"
PROJECTOR_SUBSCRIPTION = "proyector-eventos-v1"
PROJECTOR_DLQ_TOPIC = "sportapp.eventos.proyeccion.dlq"
PROJECTOR_DLQ_OPS_SUBSCRIPTION = "sportapp.eventos.proyeccion.dlq.ops"
MAX_DELIVERY_ATTEMPTS = 10
RETRY_MIN_BACKOFF_SECONDS = 10
RETRY_MAX_BACKOFF_SECONDS = 600


class EventsManagementSettings(ServiceSettings, DatabasePoolSettings):
    database_write_url: PostgresUrl
    database_read_url: PostgresUrl
    # HU021: identidad verificada con el JWKS de users-management (con caché); el comando
    # no llama a otro dominio.
    users_management_base_url: AnyHttpUrl
    # HU035: capacidad `calendar.sport.view` del deportista (contrato interno de Afiliación).
    affiliation_management_base_url: AnyHttpUrl
    upstream_timeout_seconds: float = Field(default=5.0, gt=0, le=30)
    # `iss` y `aud` que emite users-management (mismos valores por defecto).
    jwt_issuer: str = Field(default="sportapp-users-management", min_length=1, max_length=128)
    jwt_audience: str = Field(default="sportapp", min_length=1, max_length=128)
    # Ruta del contrato `actor-permissions.v1`; sin valor se usa `ACTOR_PERMISSIONS_PATH`
    # (imagen) o `contracts/authorization/` del repositorio.
    actor_permissions_path: Path | None = None


class OutboxPublisherSettings(ServiceSettings, DatabasePoolSettings):
    """`eventos-outbox-publisher`; valores iniciales conservadores de CMP-002, ajustables
    con medición (CMP-004: no cambian el contrato)."""

    database_write_url: PostgresUrl
    pubsub_project_id: Annotated[str, Field(min_length=1)]
    pubsub_topic: str = EVENTS_TOPIC
    outbox_batch_size: int = Field(default=100, ge=1, le=500)
    outbox_lease_seconds: float = Field(default=30, ge=5, le=300)
    outbox_busy_poll_seconds: float = Field(default=0.25, gt=0, le=5)
    outbox_idle_poll_max_seconds: float = Field(default=2.0, gt=0, le=30)
    worker_id: str = Field(default_factory=socket.gethostname, min_length=1, max_length=128)


class ReadProjectorSettings(ServiceSettings, DatabasePoolSettings):
    """`eventos-read-projector`: consume `proyector-eventos-v1`."""

    database_read_url: PostgresUrl
    pubsub_project_id: Annotated[str, Field(min_length=1)]
    projector_subscription: str = PROJECTOR_SUBSCRIPTION
    # Mensajes en proceso simultáneo por réplica; acota conexiones a PostgreSQL.
    consumer_max_messages: int = Field(default=10, ge=1, le=100)
