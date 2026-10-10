"""Configuración de `users_management`, validada al iniciar."""

import socket
from typing import Annotated

from pydantic import Field

from sportapp_platform.config import DatabasePoolSettings, PostgresUrl, ServiceSettings

USERS_TOPIC = "sportapp.usuarios.v1"


class UsersManagementSettings(ServiceSettings, DatabasePoolSettings):
    database_write_url: PostgresUrl
    database_read_url: PostgresUrl
    # Versión vigente de las políticas que el usuario debe aceptar al registrarse.
    registration_policies_version: Annotated[str, Field(min_length=1, max_length=32)]


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
