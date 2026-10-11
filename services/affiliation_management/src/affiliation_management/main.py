"""Aplicación FastAPI de `affiliation_management`."""

from fastapi import FastAPI
from sqlalchemy.orm import sessionmaker

from affiliation_management.api.capabilities import build_capabilities_router
from affiliation_management.application.queries.user_capabilities import GetUserCapabilities
from affiliation_management.infrastructure.clock import SystemClock
from affiliation_management.infrastructure.persistence.catalog import (
    CatalogReadinessCheck,
    SqlAlchemyCapabilitiesReader,
)
from affiliation_management.settings import AffiliationManagementSettings
from sportapp_platform.app import create_service_app
from sportapp_platform.config import load_settings
from sportapp_platform.database import DatabaseReadinessCheck, create_database_engine

SERVICE_NAME = "affiliation-management"
API_VERSION = "0.2.0"


def create_app(settings: AffiliationManagementSettings | None = None) -> FastAPI:
    settings = settings or load_settings(AffiliationManagementSettings)
    engine = create_database_engine(settings.database_url, settings)
    sessions = sessionmaker(engine, expire_on_commit=False)
    get_capabilities = GetUserCapabilities(
        reader=SqlAlchemyCapabilitiesReader(sessions), clock=SystemClock()
    )
    return create_service_app(
        service_name=SERVICE_NAME,
        title="SportApp - Afiliación",
        api_version=API_VERSION,
        settings=settings,
        readiness_checks=[
            DatabaseReadinessCheck("database", engine),
            CatalogReadinessCheck(engine, settings.affiliation_expected_catalog_version),
        ],
        routers=[build_capabilities_router(get_capabilities)],
        shutdown_hooks=[engine.dispose],
    )
