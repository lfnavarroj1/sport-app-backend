"""Configuración de `events_management`, validada al iniciar."""

import socket
from pathlib import Path
from typing import Annotated

from pydantic import AnyHttpUrl, Field, SecretStr, model_validator

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
# HU039 (ADR-045 §8): suscripción propia de `eventos-notificaciones-worker`, filtrada a
# `evento.publicado.v1`, con su DLQ. Reintentos PROPUESTOS iguales a los del proyector.
NOTIFICATIONS_SUBSCRIPTION = "eventos-notificaciones-v1"
NOTIFICATIONS_FILTER = 'attributes.event_type = "evento.publicado.v1"'
NOTIFICATIONS_DLQ_TOPIC = "sportapp.eventos.notificaciones.dlq"
NOTIFICATIONS_DLQ_OPS_SUBSCRIPTION = "sportapp.eventos.notificaciones.dlq.ops"


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
    # HU039 (ADR-045 §2): `PUT /v1/me/push-tokens` solo con FCM; la clave cifra los tokens.
    fcm_enabled: bool = False
    push_token_encryption_key: SecretStr | None = None

    @model_validator(mode="after")
    def _push_key_with_fcm(self) -> "EventsManagementSettings":
        if self.fcm_enabled and self.push_token_encryption_key is None:
            raise ValueError("FCM_ENABLED requiere PUSH_TOKEN_ENCRYPTION_KEY")
        return self


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
    """`eventos-read-projector`: consume `proyector-eventos-v1`. Con `FCM_ENABLED` (HU039)
    envía además el *push* después de proyectar la bandeja: necesita la base de escritura
    (tokens y resultados), el proyecto, la cuenta de servicio y la clave de los tokens."""

    database_read_url: PostgresUrl
    pubsub_project_id: Annotated[str, Field(min_length=1)]
    projector_subscription: str = PROJECTOR_SUBSCRIPTION
    # Mensajes en proceso simultáneo por réplica; acota conexiones a PostgreSQL.
    consumer_max_messages: int = Field(default=10, ge=1, le=100)
    fcm_enabled: bool = False
    database_write_url: PostgresUrl | None = None
    fcm_project_id: str | None = Field(default=None, min_length=1, max_length=128)
    fcm_service_account: Path | None = None
    fcm_base_url: AnyHttpUrl = AnyHttpUrl("https://fcm.googleapis.com")
    fcm_timeout_seconds: float = Field(default=5.0, gt=0, le=30)
    push_token_encryption_key: SecretStr | None = None

    @model_validator(mode="after")
    def _fcm_complete(self) -> "ReadProjectorSettings":
        required = {
            "DATABASE_WRITE_URL": self.database_write_url,
            "FCM_PROJECT_ID": self.fcm_project_id,
            "FCM_SERVICE_ACCOUNT": self.fcm_service_account,
            "PUSH_TOKEN_ENCRYPTION_KEY": self.push_token_encryption_key,
        }
        missing = [name for name, value in required.items() if value is None]
        if self.fcm_enabled and missing:
            raise ValueError("FCM_ENABLED requiere " + ", ".join(missing))
        return self


class NotificationsWorkerSettings(ServiceSettings, DatabasePoolSettings):
    """`eventos-notificaciones-worker` (HU039): consume `eventos-notificaciones-v1`, busca
    destinatarios en Usuarios y escribe notificaciones con su outbox."""

    database_write_url: PostgresUrl
    users_management_base_url: AnyHttpUrl
    upstream_timeout_seconds: float = Field(default=10.0, gt=0, le=60)
    pubsub_project_id: Annotated[str, Field(min_length=1)]
    notifications_subscription: str = NOTIFICATIONS_SUBSCRIPTION
    # Cada evento publicado puede generar miles de inserciones: pocos a la vez por réplica.
    consumer_max_messages: int = Field(default=2, ge=1, le=20)


class ReminderSchedulerSettings(ServiceSettings, DatabasePoolSettings):
    """Planificador de `EVENT_REMINDER_24H` (ADR-045 §8): una corrida cada 15 minutos."""

    database_write_url: PostgresUrl
    reminder_interval_seconds: float = Field(default=900, ge=60, le=3600)
