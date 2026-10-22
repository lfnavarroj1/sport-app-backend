"""Aplicación FastAPI de `events_management`."""

from typing import Any

import httpx
from fastapi import FastAPI
from sqlalchemy.orm import sessionmaker

from events_management.api.events import build_events_router
from events_management.api.publication import build_publication_router
from events_management.api.published_events import build_published_events_router
from events_management.api.registrations import build_registrations_router
from events_management.application.commands.create_event import CreateEvent
from events_management.application.commands.publish_event import PublishEvent
from events_management.application.commands.register_for_event import RegisterForEvent
from events_management.application.queries.my_events import ListMyEvents
from events_management.application.queries.published_events import ListPublishedEvents
from events_management.application.queries.registrations import (
    GetPublishedEvent,
    ListMyRegistrations,
)
from events_management.infrastructure.authorization import ContractPermissionPolicy
from events_management.infrastructure.clock import SystemClock
from events_management.infrastructure.integrations.affiliation import (
    AffiliationCapabilityChecker,
)
from events_management.infrastructure.persistence_read.events import (
    SqlAlchemyEventDetailReader,
    SqlAlchemyOwnEventReader,
    SqlAlchemyPublishedEventReader,
    SqlAlchemyRegistrationReader,
)
from events_management.infrastructure.persistence_write.unit_of_work import (
    SqlAlchemyEventCreationUnitOfWork,
    SqlAlchemyPublicationUnitOfWork,
    SqlAlchemyRegistrationUnitOfWork,
)
from events_management.settings import EventsManagementSettings
from sportapp_platform.app import create_service_app
from sportapp_platform.authorization import load_actor_permissions, resolve_actor_permissions_path
from sportapp_platform.config import load_settings
from sportapp_platform.database import DatabaseReadinessCheck, create_database_engine
from sportapp_platform.tokens import CachedJwksVerifier

SERVICE_NAME = "events-management"
API_VERSION = "0.6.0"


def create_app(
    settings: EventsManagementSettings | None = None,
    users_http: httpx.Client | None = None,
    affiliation_http: httpx.Client | None = None,
) -> FastAPI:
    """`users_http` y `affiliation_http` permiten inyectar transportes en pruebas de componente."""
    settings = settings or load_settings(EventsManagementSettings)
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
    return create_service_app(
        service_name=SERVICE_NAME,
        title="SportApp - Eventos",
        api_version=API_VERSION,
        settings=settings,
        # Solo la base de escritura condiciona readiness: si la lectura cae,
        # los comandos continúan y las consultas se degradan de forma controlada.
        readiness_checks=[DatabaseReadinessCheck("database_write", engine)],
        routers=[
            build_events_router(
                verify_token=verifier.verify,
                create_event=CreateEvent(
                    unit_of_work=lambda: SqlAlchemyEventCreationUnitOfWork(writes),
                    permissions=permissions,
                    clock=SystemClock(),
                ),
            ),
            build_publication_router(
                verify_token=verifier.verify,
                publish_event=PublishEvent(
                    unit_of_work=lambda: SqlAlchemyPublicationUnitOfWork(writes),
                    permissions=permissions,
                    clock=SystemClock(),
                ),
                list_my_events=ListMyEvents(reader=SqlAlchemyOwnEventReader(reads)),
            ),
            build_published_events_router(
                verify_token=verifier.verify,
                list_events=ListPublishedEvents(
                    reader=SqlAlchemyPublishedEventReader(reads),
                    capabilities=AffiliationCapabilityChecker(affiliation_http),
                    permissions=permissions,
                ),
            ),
            build_registrations_router(
                verify_token=verifier.verify,
                get_event=GetPublishedEvent(reader=SqlAlchemyEventDetailReader(reads)),
                register=RegisterForEvent(
                    unit_of_work=lambda: SqlAlchemyRegistrationUnitOfWork(writes),
                    clock=SystemClock(),
                ),
                list_registrations=ListMyRegistrations(reader=SqlAlchemyRegistrationReader(reads)),
            ),
        ],
        shutdown_hooks=[
            engine.dispose,
            read_engine.dispose,
            users_http.close,
            affiliation_http.close,
        ],
    )
