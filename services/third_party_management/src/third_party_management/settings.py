"""Configuración de `third_party_management`, validada al iniciar (HU031, ADR-042).

Las `STRAVA_*` que exige la operación son opcionales al arrancar la API: sin ellas (o con
una `STRAVA_TOKEN_KEY` que no decodifica a 32 bytes) `/health/ready` responde no listo
(`strava_configuration`) y las operaciones que las necesitan responden `503`. El worker de
sincronización sí las exige al arrancar.
"""

import socket
from datetime import timedelta
from pathlib import Path
from typing import Annotated

from pydantic import AnyHttpUrl, Field, SecretStr

from sportapp_platform.config import DatabasePoolSettings, PostgresUrl, ServiceSettings

# ADR-042 §4: tópico de `actividad.importada.v1`.
TERCEROS_TOPIC = "sportapp.terceros.v1"
# Mismos parámetros de outbox que los demás dominios (CMP-002): los publicados se conservan
# siete días; espera de 10 a 600 s y alerta desde el décimo fallo.
PUBLISHED_RETENTION = timedelta(days=7)
PURGE_INTERVAL = timedelta(hours=1)
RETRY_MIN_SECONDS = 10.0
RETRY_MAX_SECONDS = 600.0
ALERT_FROM_ATTEMPT = 10

DEFAULT_STRAVA_BASE_URL = "https://www.strava.com"
DEFAULT_STRAVA_AUTHORIZE_URL = "https://www.strava.com/oauth/mobile/authorize"

HttpUrlText = Annotated[str, Field(pattern=r"^https?://\S+$", max_length=512)]


class StravaSettings(ServiceSettings):
    """Variables de Strava comunes a la API y al worker. `STRAVA_REDIRECT_URI` se usa tal
    cual (Strava exige la URI exacta), por eso no se normaliza como URL."""

    strava_client_id: str | None = Field(default=None, max_length=64)
    strava_client_secret: SecretStr | None = None
    strava_token_key: SecretStr | None = None
    strava_base_url: HttpUrlText = DEFAULT_STRAVA_BASE_URL


class ThirdPartyManagementSettings(StravaSettings, DatabasePoolSettings):
    database_url: PostgresUrl
    # Identidad desde el JWKS de users-management.
    users_management_base_url: AnyHttpUrl
    # Capacidad `integrations.strava.connect` (contrato interno de Afiliación).
    affiliation_management_base_url: AnyHttpUrl
    upstream_timeout_seconds: float = Field(default=5.0, gt=0, le=30)
    # `iss` y `aud` que emite users-management (mismos valores por defecto).
    jwt_issuer: str = Field(default="sportapp-users-management", min_length=1, max_length=128)
    jwt_audience: str = Field(default="sportapp", min_length=1, max_length=128)
    # Ruta del contrato `actor-permissions`; HU031 solo admite `athlete`, sin permisos por
    # actor, así que la API no la carga (se conserva por uniformidad de configuración).
    actor_permissions_path: Path | None = None
    strava_redirect_uri: HttpUrlText | None = None
    strava_consent_version: str | None = Field(default=None, max_length=64)
    strava_authorize_url: HttpUrlText = DEFAULT_STRAVA_AUTHORIZE_URL


class OutboxPublisherSettings(ServiceSettings, DatabasePoolSettings):
    """`terceros-outbox-publisher`; mismos valores iniciales conservadores que los
    publicadores de los demás dominios, ajustables con medición sin cambiar el contrato."""

    database_url: PostgresUrl
    pubsub_project_id: Annotated[str, Field(min_length=1)]
    pubsub_topic: str = TERCEROS_TOPIC
    outbox_batch_size: int = Field(default=100, ge=1, le=500)
    outbox_lease_seconds: float = Field(default=30, ge=5, le=300)
    outbox_busy_poll_seconds: float = Field(default=0.25, gt=0, le=5)
    outbox_idle_poll_max_seconds: float = Field(default=2.0, gt=0, le=30)
    worker_id: str = Field(default_factory=socket.gethostname, min_length=1, max_length=128)


class SyncWorkerSettings(StravaSettings, DatabasePoolSettings):
    """`terceros-sync-worker`. El arrendamiento (300 s) supera el peor caso de una corrida:
    3 páginas con 2 reintentos de hasta 5 s más la renovación del token."""

    database_url: PostgresUrl
    strava_client_id: str = Field(min_length=1, max_length=64)
    strava_client_secret: SecretStr
    strava_token_key: SecretStr
    sync_lease_seconds: float = Field(default=300, ge=60, le=1800)
    sync_busy_poll_seconds: float = Field(default=0.25, gt=0, le=5)
    sync_idle_poll_max_seconds: float = Field(default=2.0, gt=0, le=30)
    worker_id: str = Field(default_factory=socket.gethostname, min_length=1, max_length=128)
