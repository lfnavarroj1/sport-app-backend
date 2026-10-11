"""Aplicación FastAPI de `events_management`."""

from fastapi import FastAPI

from events_management.settings import EventsManagementSettings
from sportapp_platform.app import create_service_app
from sportapp_platform.config import load_settings
from sportapp_platform.database import DatabaseReadinessCheck, create_database_engine

SERVICE_NAME = "events-management"
API_VERSION = "0.1.0"


def create_app(settings: EventsManagementSettings | None = None) -> FastAPI:
    settings = settings or load_settings(EventsManagementSettings)
    engine = create_database_engine(settings.database_write_url, settings)
    return create_service_app(
        service_name=SERVICE_NAME,
        title="SportApp - Eventos",
        api_version=API_VERSION,
        settings=settings,
        # Solo la base de escritura condiciona readiness: si la lectura cae,
        # los comandos continúan y las consultas se degradan de forma controlada.
        readiness_checks=[DatabaseReadinessCheck("database_write", engine)],
        shutdown_hooks=[engine.dispose],
    )
