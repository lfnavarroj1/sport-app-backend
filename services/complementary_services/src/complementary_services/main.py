"""Aplicación FastAPI de `complementary_services`."""

from fastapi import FastAPI

from complementary_services.settings import ComplementaryServicesSettings
from sportapp_platform.app import create_service_app
from sportapp_platform.config import load_settings
from sportapp_platform.database import DatabaseReadinessCheck, create_database_engine

SERVICE_NAME = "complementary-services"
API_VERSION = "0.1.0"


def create_app(settings: ComplementaryServicesSettings | None = None) -> FastAPI:
    settings = settings or load_settings(ComplementaryServicesSettings)
    engine = create_database_engine(settings.database_url, settings)
    return create_service_app(
        service_name=SERVICE_NAME,
        title="SportApp - Servicios complementarios",
        api_version=API_VERSION,
        settings=settings,
        readiness_checks=[DatabaseReadinessCheck("database", engine)],
        shutdown_hooks=[engine.dispose],
    )
