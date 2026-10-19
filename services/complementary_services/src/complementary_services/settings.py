"""Configuración de `complementary_services`, validada al iniciar."""

import socket
from datetime import timedelta
from pathlib import Path
from typing import Annotated

from pydantic import AnyHttpUrl, Field

from sportapp_platform.config import DatabasePoolSettings, PostgresUrl, ServiceSettings

# Mensajería de HU019 aprobada por ADR-027 para `servicio.publicado.v1`: solo el tópico;
# no hay proyector ni suscripción consumidora (cada consumidor futuro necesita contrato).
SERVICES_TOPIC = "sportapp.servicios.v1"
# ADR-027: un evento publicado se conserva siete días; un pendiente nunca se elimina.
PUBLISHED_RETENTION = timedelta(days=7)
# La purga de publicados corre como máximo una vez por hora en cada réplica.
PURGE_INTERVAL = timedelta(hours=1)
# ADR-027: espera progresiva de 10 a 600 segundos y alerta desde el décimo fallo.
RETRY_MIN_SECONDS = 10.0
RETRY_MAX_SECONDS = 600.0
ALERT_FROM_ATTEMPT = 10


class ComplementaryServicesSettings(ServiceSettings, DatabasePoolSettings):
    database_url: PostgresUrl
    # HU015: identidad desde el JWKS de users-management.
    users_management_base_url: AnyHttpUrl
    # HU020: capacidad `services.sports_medicine.access` (contrato interno de Afiliación).
    affiliation_management_base_url: AnyHttpUrl
    upstream_timeout_seconds: float = Field(default=5.0, gt=0, le=30)
    # `iss` y `aud` que emite users-management (mismos valores por defecto).
    jwt_issuer: str = Field(default="sportapp-users-management", min_length=1, max_length=128)
    jwt_audience: str = Field(default="sportapp", min_length=1, max_length=128)
    # Ruta del contrato `actor-permissions.v1`; sin valor se usa `ACTOR_PERMISSIONS_PATH`
    # (imagen) o `contracts/authorization/` del repositorio.
    actor_permissions_path: Path | None = None


class OutboxPublisherSettings(ServiceSettings, DatabasePoolSettings):
    """`servicios-outbox-publisher` (HU019); mismos valores iniciales conservadores que
    los publicadores de los demás dominios, ajustables con medición sin cambiar el
    contrato."""

    database_url: PostgresUrl
    pubsub_project_id: Annotated[str, Field(min_length=1)]
    pubsub_topic: str = SERVICES_TOPIC
    outbox_batch_size: int = Field(default=100, ge=1, le=500)
    outbox_lease_seconds: float = Field(default=30, ge=5, le=300)
    outbox_busy_poll_seconds: float = Field(default=0.25, gt=0, le=5)
    outbox_idle_poll_max_seconds: float = Field(default=2.0, gt=0, le=30)
    worker_id: str = Field(default_factory=socket.gethostname, min_length=1, max_length=128)
