"""Aplicación FastAPI de `third_party_management` (HU031, ADR-042)."""

from typing import Any

import httpx
from fastapi import FastAPI
from sqlalchemy.orm import sessionmaker

from sportapp_platform.app import create_service_app
from sportapp_platform.config import load_settings
from sportapp_platform.database import DatabaseReadinessCheck, create_database_engine
from sportapp_platform.tokens import CachedJwksVerifier
from third_party_management.api.strava import build_strava_router
from third_party_management.application.connections import GetIntegrationStatus, UnlinkStrava
from third_party_management.application.oauth import CompleteCallback, StartAuthorization
from third_party_management.application.syncs import RequestSync
from third_party_management.infrastructure.clock import SystemClock
from third_party_management.infrastructure.integrations.affiliation import (
    AffiliationCapabilityChecker,
)
from third_party_management.infrastructure.persistence.unit_of_work import SqlAlchemyUnitOfWork
from third_party_management.infrastructure.strava.client import STRAVA_TIMEOUT
from third_party_management.infrastructure.strava.configuration import (
    StravaConfigurationCheck,
    build_strava_context,
)
from third_party_management.settings import ThirdPartyManagementSettings

SERVICE_NAME = "third-party-management"
API_VERSION = "0.2.0"


def create_app(
    settings: ThirdPartyManagementSettings | None = None,
    users_http: httpx.Client | None = None,
    affiliation_http: httpx.Client | None = None,
    strava_http: httpx.Client | None = None,
) -> FastAPI:
    """`users_http`, `affiliation_http` y `strava_http` permiten inyectar transportes en
    pruebas de componente (por ejemplo el simulador de Strava)."""
    settings = settings or load_settings(ThirdPartyManagementSettings)
    engine = create_database_engine(settings.database_url, settings)
    sessions = sessionmaker(engine, expire_on_commit=False)
    users_http = users_http or httpx.Client(
        base_url=str(settings.users_management_base_url),
        timeout=settings.upstream_timeout_seconds,
    )
    affiliation_http = affiliation_http or httpx.Client(
        base_url=str(settings.affiliation_management_base_url),
        timeout=settings.upstream_timeout_seconds,
    )
    strava_http = strava_http or httpx.Client(
        base_url=settings.strava_base_url, timeout=STRAVA_TIMEOUT
    )

    def fetch_jwks() -> dict[str, Any]:
        response = users_http.get("/.well-known/jwks.json")
        response.raise_for_status()
        document: dict[str, Any] = response.json()
        return document

    verifier = CachedJwksVerifier(
        fetch_jwks, issuer=settings.jwt_issuer, audience=settings.jwt_audience
    )
    clock = SystemClock()
    capabilities = AffiliationCapabilityChecker(affiliation_http)
    strava = build_strava_context(settings, strava_http)

    def unit_of_work() -> SqlAlchemyUnitOfWork:
        return SqlAlchemyUnitOfWork(sessions)

    return create_service_app(
        service_name=SERVICE_NAME,
        title="SportApp - Gestión de terceros",
        api_version=API_VERSION,
        settings=settings,
        readiness_checks=[
            DatabaseReadinessCheck("database", engine),
            StravaConfigurationCheck(settings),
        ],
        routers=[
            build_strava_router(
                verify_token=verifier.verify,
                start_authorization=StartAuthorization(
                    unit_of_work=unit_of_work,
                    capabilities=capabilities,
                    strava=strava,
                    clock=clock,
                ),
                complete_callback=CompleteCallback(
                    unit_of_work=unit_of_work, strava=strava, clock=clock
                ),
                get_status=GetIntegrationStatus(
                    unit_of_work=unit_of_work, capabilities=capabilities
                ),
                unlink=UnlinkStrava(
                    unit_of_work=unit_of_work,
                    capabilities=capabilities,
                    strava=strava,
                    clock=clock,
                ),
                request_sync=RequestSync(
                    unit_of_work=unit_of_work,
                    capabilities=capabilities,
                    strava=strava,
                    clock=clock,
                ),
            )
        ],
        shutdown_hooks=[
            engine.dispose,
            users_http.close,
            affiliation_http.close,
            strava_http.close,
        ],
    )
