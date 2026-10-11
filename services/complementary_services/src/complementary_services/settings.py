"""Configuración de `complementary_services`, validada al iniciar."""

from sportapp_platform.config import DatabasePoolSettings, PostgresUrl, ServiceSettings


class ComplementaryServicesSettings(ServiceSettings, DatabasePoolSettings):
    database_url: PostgresUrl
