"""Configuración de `events_management`, validada al iniciar."""

from sportapp_platform.config import DatabasePoolSettings, PostgresUrl, ServiceSettings


class EventsManagementSettings(ServiceSettings, DatabasePoolSettings):
    database_write_url: PostgresUrl
    database_read_url: PostgresUrl
