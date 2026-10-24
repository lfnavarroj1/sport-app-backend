"""Aplicación FastAPI de `affiliation_management`."""

from typing import Any

import httpx
from fastapi import FastAPI
from sqlalchemy.orm import sessionmaker

from affiliation_management.api.account_statement import build_account_statement_router
from affiliation_management.api.capabilities import build_capabilities_router
from affiliation_management.api.subscriptions import build_subscriptions_router
from affiliation_management.application.commands.change_subscription import ChangeSubscription
from affiliation_management.application.queries.account_statement import GetMyAccountStatement
from affiliation_management.application.queries.subscriptions import GetMySubscription, ListPlans
from affiliation_management.application.queries.user_capabilities import GetUserCapabilities
from affiliation_management.infrastructure.clock import SystemClock
from affiliation_management.infrastructure.persistence.account_statement import (
    SqlAlchemyAccountStatementReader,
)
from affiliation_management.infrastructure.persistence.catalog import (
    CatalogReadinessCheck,
    SqlAlchemyCapabilitiesReader,
)
from affiliation_management.infrastructure.persistence.subscriptions import (
    SqlAlchemyCatalogReader,
    SqlAlchemySubscriptionChangeUnitOfWork,
    SqlAlchemySubscriptionReader,
)
from affiliation_management.settings import AffiliationManagementSettings
from sportapp_platform.app import create_service_app
from sportapp_platform.config import load_settings
from sportapp_platform.database import DatabaseReadinessCheck, create_database_engine
from sportapp_platform.tokens import CachedJwksVerifier

SERVICE_NAME = "affiliation-management"
API_VERSION = "0.4.0"


def create_app(
    settings: AffiliationManagementSettings | None = None,
    users_http: httpx.Client | None = None,
) -> FastAPI:
    """`users_http` permite inyectar el transporte de Usuarios en pruebas de componente."""
    settings = settings or load_settings(AffiliationManagementSettings)
    engine = create_database_engine(settings.database_url, settings)
    sessions = sessionmaker(engine, expire_on_commit=False)
    clock = SystemClock()
    users_http = users_http or httpx.Client(
        base_url=str(settings.users_management_base_url),
        timeout=settings.upstream_timeout_seconds,
    )

    def fetch_jwks() -> dict[str, Any]:
        response = users_http.get("/.well-known/jwks.json")
        response.raise_for_status()
        document: dict[str, Any] = response.json()
        return document

    verifier = CachedJwksVerifier(
        fetch_jwks, issuer=settings.jwt_issuer, audience=settings.jwt_audience
    )
    get_capabilities = GetUserCapabilities(
        reader=SqlAlchemyCapabilitiesReader(sessions), clock=clock
    )
    catalog = SqlAlchemyCatalogReader(sessions)
    subscriptions = SqlAlchemySubscriptionReader(sessions)
    return create_service_app(
        service_name=SERVICE_NAME,
        title="SportApp - Afiliación",
        api_version=API_VERSION,
        settings=settings,
        readiness_checks=[
            DatabaseReadinessCheck("database", engine),
            CatalogReadinessCheck(engine, settings.affiliation_expected_catalog_version),
        ],
        routers=[
            build_capabilities_router(get_capabilities),
            build_subscriptions_router(
                verify_token=verifier.verify,
                list_plans=ListPlans(
                    catalog=catalog,
                    subscriptions=subscriptions,
                    catalog_version=settings.affiliation_expected_catalog_version,
                ),
                get_subscription=GetMySubscription(subscriptions),
                change_subscription=ChangeSubscription(
                    unit_of_work=lambda: SqlAlchemySubscriptionChangeUnitOfWork(sessions),
                    catalog=catalog,
                    clock=clock,
                ),
            ),
            build_account_statement_router(
                verify_token=verifier.verify,
                get_statement=GetMyAccountStatement(
                    reader=SqlAlchemyAccountStatementReader(sessions), clock=clock
                ),
            ),
        ],
        shutdown_hooks=[engine.dispose, users_http.close],
    )
