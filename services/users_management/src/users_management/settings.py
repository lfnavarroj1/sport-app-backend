"""Configuración de `users_management`, validada al iniciar."""

import socket
from datetime import timedelta
from typing import Annotated

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import AfterValidator, Field, SecretStr

from sportapp_platform.config import DatabasePoolSettings, PostgresUrl, ServiceSettings
from users_management.infrastructure.security.access_tokens import (
    load_signing_key,
    normalize_private_key_pem,
)

USERS_TOPIC = "sportapp.usuarios.v1"
# `usuarios-read-projector` (CMP-001, HU001 refactor y HU005): reintentos y entregas de
# CMP-001, no copiados de otros dominios.
PROJECTOR_SUBSCRIPTION = "proyector-usuarios-v1"
PROJECTOR_DLQ_TOPIC = "sportapp.usuarios.proyeccion.dlq"
PROJECTOR_DLQ_OPS_SUBSCRIPTION = "sportapp.usuarios.proyeccion.dlq.ops"
MAX_DELIVERY_ATTEMPTS = 5
RETRY_MIN_BACKOFF_SECONDS = 10
RETRY_MAX_BACKOFF_SECONDS = 600


def _require_ed25519_private_key(value: SecretStr) -> SecretStr:
    load_signing_key(value.get_secret_value())
    return SecretStr(normalize_private_key_pem(value.get_secret_value()))


SigningKeyPem = Annotated[SecretStr, AfterValidator(_require_ed25519_private_key)]


class UsersManagementSettings(ServiceSettings, DatabasePoolSettings):
    database_write_url: PostgresUrl
    database_read_url: PostgresUrl
    # Versión vigente de las políticas que el usuario debe aceptar al registrarse.
    registration_policies_version: Annotated[str, Field(min_length=1, max_length=32)]
    # HU005 (ADR-034): versión vigente del consentimiento de datos de salud; sin ella la
    # aplicación no arranca.
    health_data_consent_version: Annotated[str, Field(min_length=1, max_length=64)]
    # HU003 (aprobado el 2026-10-10): access JWT EdDSA y refresh token rotativo.
    jwt_private_key: SigningKeyPem
    jwt_issuer: str = Field(default="sportapp-users-management", min_length=1, max_length=128)
    jwt_audience: str = Field(default="sportapp", min_length=1, max_length=128)
    access_token_ttl_seconds: int = Field(default=900, ge=60, le=3600)
    refresh_token_ttl_seconds: int = Field(default=7 * 24 * 3600, ge=3600, le=30 * 24 * 3600)
    # Control de abuso: máximo de fallos por correo + IP dentro de la ventana.
    login_max_failures: int = Field(default=5, ge=1, le=100)
    login_failure_window_seconds: int = Field(default=900, ge=60, le=24 * 3600)

    def signing_key(self) -> Ed25519PrivateKey:
        return load_signing_key(self.jwt_private_key.get_secret_value())

    @property
    def access_token_ttl(self) -> timedelta:
        return timedelta(seconds=self.access_token_ttl_seconds)

    @property
    def refresh_token_ttl(self) -> timedelta:
        return timedelta(seconds=self.refresh_token_ttl_seconds)

    @property
    def login_failure_window(self) -> timedelta:
        return timedelta(seconds=self.login_failure_window_seconds)


class OutboxPublisherSettings(ServiceSettings, DatabasePoolSettings):
    """`usuarios-outbox-publisher`; valores iniciales de CMP-001 sujetos a prueba."""

    database_write_url: PostgresUrl
    pubsub_project_id: Annotated[str, Field(min_length=1)]
    pubsub_topic: str = USERS_TOPIC
    outbox_batch_size: int = Field(default=100, ge=1, le=500)
    outbox_lease_seconds: float = Field(default=30, ge=5, le=300)
    outbox_busy_poll_seconds: float = Field(default=0.25, gt=0, le=5)
    outbox_idle_poll_max_seconds: float = Field(default=2.0, gt=0, le=30)
    worker_id: str = Field(default_factory=socket.gethostname, min_length=1, max_length=128)


class ReadProjectorSettings(ServiceSettings, DatabasePoolSettings):
    """`usuarios-read-projector`: consume `proyector-usuarios-v1` (CMP-001)."""

    database_read_url: PostgresUrl
    pubsub_project_id: Annotated[str, Field(min_length=1)]
    projector_subscription: str = PROJECTOR_SUBSCRIPTION
    # Mensajes en proceso simultáneo por réplica; acota conexiones a PostgreSQL (CMP-001: 5).
    consumer_max_messages: int = Field(default=5, ge=1, le=100)
