"""Configuración de `affiliation_management`, validada al iniciar."""

from sportapp_platform.config import DatabasePoolSettings, PostgresUrl, ServiceSettings


class AffiliationManagementSettings(ServiceSettings, DatabasePoolSettings):
    database_url: PostgresUrl
