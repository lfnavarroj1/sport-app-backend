"""Configuración de `affiliation_management`, validada al iniciar."""

import socket
from typing import Annotated

from pydantic import AnyHttpUrl, Field

from sportapp_platform.config import DatabasePoolSettings, PostgresUrl, ServiceSettings

# Contratos de mensajería aprobados en ADR-024 para el alta gratuita de HU001.
USERS_TOPIC = "sportapp.usuarios.v1"
AFFILIATION_TOPIC = "sportapp.afiliacion.v1"
FREE_PLAN_SUBSCRIPTION = "sportapp.afiliacion.alta-gratuita.v1"
# R-19 (ADR-034): la suscripción solo recibe `usuario.registrado.v1` del tópico de usuarios.
FREE_PLAN_FILTER = 'attributes.event_type = "usuario.registrado.v1"'
FREE_PLAN_DLQ_TOPIC = "sportapp.afiliacion.alta-gratuita.dlq"
FREE_PLAN_DLQ_OPS_SUBSCRIPTION = "sportapp.afiliacion.alta-gratuita.dlq.ops"
MAX_DELIVERY_ATTEMPTS = 10
RETRY_MIN_BACKOFF_SECONDS = 10
RETRY_MAX_BACKOFF_SECONDS = 600

CatalogVersion = Annotated[str, Field(min_length=1, max_length=40)]


class AffiliationManagementSettings(ServiceSettings, DatabasePoolSettings):
    database_url: PostgresUrl
    # Versión de catálogo que debe estar instalada para quedar listo (ADR-024).
    affiliation_expected_catalog_version: CatalogVersion
    # HU016: el servicio verifica el access token del deportista con el JWKS de
    # users-management (decisión del responsable del 2026-10-23); el contrato interno de
    # capacidades sigue sin token.
    users_management_base_url: AnyHttpUrl
    upstream_timeout_seconds: float = Field(default=5.0, gt=0, le=30)
    jwt_issuer: str = Field(default="sportapp-users-management", min_length=1, max_length=128)
    jwt_audience: str = Field(default="sportapp", min_length=1, max_length=128)


class FreePlanConsumerSettings(ServiceSettings, DatabasePoolSettings):
    """Consumidor de `usuario.registrado.v1` que aprovisiona el plan gratuito."""

    database_url: PostgresUrl
    pubsub_project_id: Annotated[str, Field(min_length=1)]
    free_plan_subscription: str = FREE_PLAN_SUBSCRIPTION
    # Mensajes en proceso simultáneo por réplica; acota conexiones a PostgreSQL.
    consumer_max_messages: int = Field(default=10, ge=1, le=100)


class OutboxPublisherSettings(ServiceSettings, DatabasePoolSettings):
    """`afiliacion-outbox-publisher`; mismos valores iniciales que el publicador de usuarios."""

    database_url: PostgresUrl
    pubsub_project_id: Annotated[str, Field(min_length=1)]
    pubsub_topic: str = AFFILIATION_TOPIC
    outbox_batch_size: int = Field(default=100, ge=1, le=500)
    outbox_lease_seconds: float = Field(default=30, ge=5, le=300)
    outbox_busy_poll_seconds: float = Field(default=0.25, gt=0, le=5)
    outbox_idle_poll_max_seconds: float = Field(default=2.0, gt=0, le=30)
    worker_id: str = Field(default_factory=socket.gethostname, min_length=1, max_length=128)
