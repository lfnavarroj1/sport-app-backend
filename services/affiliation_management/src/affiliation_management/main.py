"""Aplicación FastAPI de `affiliation_management`."""

from fastapi import FastAPI

from affiliation_management.settings import AffiliationManagementSettings
from sportapp_platform.app import create_service_app
from sportapp_platform.config import load_settings
from sportapp_platform.database import DatabaseReadinessCheck, create_database_engine

SERVICE_NAME = "affiliation-management"
API_VERSION = "0.1.0"


def create_app(settings: AffiliationManagementSettings | None = None) -> FastAPI:
    settings = settings or load_settings(AffiliationManagementSettings)
    engine = create_database_engine(settings.database_url, settings)
    return create_service_app(
        service_name=SERVICE_NAME,
        title="SportApp - Afiliación",
        api_version=API_VERSION,
        settings=settings,
        readiness_checks=[DatabaseReadinessCheck("database", engine)],
        shutdown_hooks=[engine.dispose],
    )
