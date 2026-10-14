"""Aplicación FastAPI de `complementary_services`."""

from typing import Any

import httpx
from fastapi import FastAPI
from sqlalchemy.orm import sessionmaker

from complementary_services.api.publication import build_publication_router
from complementary_services.api.services import build_services_router
from complementary_services.application.commands.create_service import CreateService
from complementary_services.application.commands.publish_service import PublishService
from complementary_services.application.queries.my_services import GetMyService, ListMyServices
from complementary_services.infrastructure.authorization import ContractPermissionPolicy
from complementary_services.infrastructure.clock import SystemClock
from complementary_services.infrastructure.persistence.services_reader import (
    SqlAlchemyServiceReader,
)
from complementary_services.infrastructure.persistence.unit_of_work import (
    SqlAlchemyCreationUnitOfWork,
    SqlAlchemyPublicationUnitOfWork,
)
from complementary_services.settings import ComplementaryServicesSettings
from sportapp_platform.app import create_service_app
from sportapp_platform.authorization import load_actor_permissions, resolve_actor_permissions_path
from sportapp_platform.config import load_settings
from sportapp_platform.database import DatabaseReadinessCheck, create_database_engine
from sportapp_platform.tokens import CachedJwksVerifier

SERVICE_NAME = "complementary-services"
API_VERSION = "0.3.0"


def create_app(
    settings: ComplementaryServicesSettings | None = None,
    users_http: httpx.Client | None = None,
) -> FastAPI:
    """`users_http` permite inyectar el transporte del JWKS en pruebas de componente."""
    settings = settings or load_settings(ComplementaryServicesSettings)
    permissions = ContractPermissionPolicy(
        load_actor_permissions(settings.actor_permissions_path or resolve_actor_permissions_path())
    )
    engine = create_database_engine(settings.database_url, settings)
    sessions = sessionmaker(engine, expire_on_commit=False)
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
    clock = SystemClock()
    reader = SqlAlchemyServiceReader(sessions)
    return create_service_app(
        service_name=SERVICE_NAME,
        title="SportApp - Servicios complementarios",
        api_version=API_VERSION,
        settings=settings,
        readiness_checks=[DatabaseReadinessCheck("database", engine)],
        routers=[
            build_services_router(
                verify_token=verifier.verify,
                create_service=CreateService(
                    unit_of_work=lambda: SqlAlchemyCreationUnitOfWork(sessions),
                    permissions=permissions,
                    clock=clock,
                ),
            ),
            build_publication_router(
                verify_token=verifier.verify,
                publish_service=PublishService(
                    unit_of_work=lambda: SqlAlchemyPublicationUnitOfWork(sessions),
                    permissions=permissions,
                    clock=clock,
                ),
                list_my_services=ListMyServices(reader=reader),
                get_my_service=GetMyService(reader=reader),
            ),
        ],
        shutdown_hooks=[engine.dispose, users_http.close],
    )
