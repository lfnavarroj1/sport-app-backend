"""Configuración de `training_management`, validada al iniciar."""

from sportapp_platform.config import DatabasePoolSettings, PostgresUrl, ServiceSettings


class TrainingManagementSettings(ServiceSettings, DatabasePoolSettings):
    database_write_url: PostgresUrl
    database_read_url: PostgresUrl
