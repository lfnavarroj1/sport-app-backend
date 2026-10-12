"""Aplicación FastAPI de `training_management`."""

from typing import Any

import httpx
from fastapi import FastAPI
from sqlalchemy.orm import sessionmaker

from sportapp_platform.app import create_service_app
from sportapp_platform.authorization import load_actor_permissions, resolve_actor_permissions_path
from sportapp_platform.config import load_settings
from sportapp_platform.database import DatabaseReadinessCheck, create_database_engine
from sportapp_platform.tokens import CachedJwksVerifier
from training_management.api.authorizations import build_authorizations_router
from training_management.api.history import build_history_router
from training_management.api.plans import build_plans_router
from training_management.application.commands.authorizations import (
    GrantAuthorization,
    RevokeAuthorization,
)
from training_management.application.commands.create_training_plan import CreateTrainingPlan
from training_management.application.queries.get_training_plan import GetTrainingPlan
from training_management.application.queries.training_history import GetTrainingHistory
from training_management.infrastructure.authorization import ContractPermissionPolicy
from training_management.infrastructure.clock import SystemClock
from training_management.infrastructure.integrations.affiliation import (
    AffiliationCapabilityChecker,
)
from training_management.infrastructure.persistence_read.history import (
    SqlAlchemyAuthorizationReader,
    SqlAlchemyHistoryReader,
)
from training_management.infrastructure.persistence_read.plans import SqlAlchemyPlanReader
from training_management.infrastructure.persistence_write.unit_of_work import (
    SqlAlchemyAuthorizationUnitOfWork,
    SqlAlchemyPlanningUnitOfWork,
)
from training_management.settings import TrainingManagementSettings

SERVICE_NAME = "training-management"
API_VERSION = "0.3.0"


def create_app(
    settings: TrainingManagementSettings | None = None,
    users_http: httpx.Client | None = None,
    affiliation_http: httpx.Client | None = None,
) -> FastAPI:
    """`users_http` y `affiliation_http` permiten inyectar transportes en pruebas de componente."""
    settings = settings or load_settings(TrainingManagementSettings)
    permissions = ContractPermissionPolicy(
        load_actor_permissions(settings.actor_permissions_path or resolve_actor_permissions_path())
    )
    engine = create_database_engine(settings.database_write_url, settings)
    read_engine = create_database_engine(settings.database_read_url, settings)
    writes = sessionmaker(engine, expire_on_commit=False)
    reads = sessionmaker(read_engine, expire_on_commit=False)
    users_http = users_http or httpx.Client(
        base_url=str(settings.users_management_base_url),
        timeout=settings.upstream_timeout_seconds,
    )
    affiliation_http = affiliation_http or httpx.Client(
        base_url=str(settings.affiliation_management_base_url),
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
    capabilities = AffiliationCapabilityChecker(affiliation_http)
    return create_service_app(
        service_name=SERVICE_NAME,
        title="SportApp - Entrenamientos",
        api_version=API_VERSION,
        settings=settings,
        # Solo la base de escritura condiciona readiness: si la lectura cae,
        # los comandos continúan y las consultas se degradan de forma controlada.
        readiness_checks=[DatabaseReadinessCheck("database_write", engine)],
        routers=[
            build_authorizations_router(
                verify_token=verifier.verify,
                grant=GrantAuthorization(
                    unit_of_work=lambda: SqlAlchemyAuthorizationUnitOfWork(writes),
                    capabilities=capabilities,
                    clock=clock,
                ),
                revoke=RevokeAuthorization(
                    unit_of_work=lambda: SqlAlchemyAuthorizationUnitOfWork(writes), clock=clock
                ),
            ),
            build_plans_router(
                verify_token=verifier.verify,
                create_plan=CreateTrainingPlan(
                    unit_of_work=lambda: SqlAlchemyPlanningUnitOfWork(writes),
                    permissions=permissions,
                    clock=clock,
                ),
                get_plan=GetTrainingPlan(
                    reader=SqlAlchemyPlanReader(reads),
                    permissions=permissions,
                    capabilities=capabilities,
                ),
            ),
            build_history_router(
                verify_token=verifier.verify,
                get_history=GetTrainingHistory(
                    reader=SqlAlchemyHistoryReader(reads),
                    authorizations=SqlAlchemyAuthorizationReader(reads),
                    permissions=permissions,
                ),
            ),
        ],
        shutdown_hooks=[
            engine.dispose,
            read_engine.dispose,
            users_http.close,
            affiliation_http.close,
        ],
    )
