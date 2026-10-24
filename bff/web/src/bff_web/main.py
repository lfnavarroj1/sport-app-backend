"""Aplicación FastAPI de `bff_web`."""

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from bff_web.api.capabilities import build_capabilities_router
from bff_web.api.event_attendees import build_event_attendees_router
from bff_web.api.event_calendar import build_event_calendar_router
from bff_web.api.event_fee import build_event_fee_router
from bff_web.api.event_publication import build_event_publication_router
from bff_web.api.events import build_events_router
from bff_web.api.integrated_calendar import build_integrated_calendar_router
from bff_web.api.nutrition import build_nutrition_router
from bff_web.api.profile import build_profile_router
from bff_web.api.registration import (
    IDEMPOTENCY_HEADER,
    REPLAYED_HEADER,
    build_registration_router,
)
from bff_web.api.service_associations import build_service_associations_router
from bff_web.api.services import build_service_availability_router, build_services_router
from bff_web.api.sessions import RETRY_AFTER_HEADER, RefreshCookie, build_sessions_router
from bff_web.api.subscriptions import build_subscriptions_router
from bff_web.api.training import build_training_router
from bff_web.api.training_history import build_training_history_router
from bff_web.api.training_plan_templates import build_training_plan_templates_router
from bff_web.api.training_routes import build_training_routes_router
from bff_web.clients.affiliation_management import AffiliationManagementClient
from bff_web.clients.complementary_services import ComplementaryServicesClient
from bff_web.clients.events_management import EventsManagementClient
from bff_web.clients.training_management import IF_NONE_MATCH_HEADER, TrainingManagementClient
from bff_web.clients.users_management import UsersManagementClient
from bff_web.settings import BffWebSettings
from sportapp_platform.app import create_service_app
from sportapp_platform.authorization import load_actor_permissions, resolve_actor_permissions_path
from sportapp_platform.config import load_settings
from sportapp_platform.correlation import CORRELATION_HEADER
from sportapp_platform.tokens import CachedJwksVerifier

SERVICE_NAME = "bff-web"
API_VERSION = "0.27.0"
CHANNEL = "web"

# Lo que el navegador puede enviar y leer en llamadas de otro origen.
CORS_ALLOWED_METHODS = ("GET", "POST", "PUT", "DELETE")
CORS_ALLOWED_HEADERS = (
    "Authorization",
    "Content-Type",
    IDEMPOTENCY_HEADER,
    CORRELATION_HEADER,
    IF_NONE_MATCH_HEADER,
)
# `ETag` e `If-None-Match`: revalidación del catálogo de alimentos con `304` (HU014).
CORS_EXPOSED_HEADERS = (
    "Location",
    REPLAYED_HEADER,
    RETRY_AFTER_HEADER,
    CORRELATION_HEADER,
    "ETag",
)


def create_app(
    settings: BffWebSettings | None = None,
    *,
    users_http: httpx.Client | None = None,
    affiliation_http: httpx.Client | None = None,
    training_http: httpx.Client | None = None,
    services_http: httpx.Client | None = None,
    events_http: httpx.Client | None = None,
) -> FastAPI:
    """Los transportes `*_http` permiten inyectar servicios en pruebas de componente."""
    settings = settings or load_settings(BffWebSettings)
    permissions = load_actor_permissions(
        settings.actor_permissions_path or resolve_actor_permissions_path()
    )
    users_http = users_http or httpx.Client(
        base_url=str(settings.users_management_base_url),
        timeout=settings.upstream_timeout_seconds,
    )
    affiliation_http = affiliation_http or httpx.Client(
        base_url=str(settings.affiliation_management_base_url),
        timeout=settings.upstream_timeout_seconds,
    )
    training_http = training_http or httpx.Client(
        base_url=str(settings.training_management_base_url),
        timeout=settings.upstream_timeout_seconds,
    )
    services_http = services_http or httpx.Client(
        base_url=str(settings.complementary_services_base_url),
        timeout=settings.upstream_timeout_seconds,
    )
    events_http = events_http or httpx.Client(
        base_url=str(settings.events_management_base_url),
        timeout=settings.upstream_timeout_seconds,
    )
    users = UsersManagementClient(users_http)
    events = EventsManagementClient(events_http)
    training = TrainingManagementClient(training_http)
    affiliation = AffiliationManagementClient(affiliation_http)
    verifier = CachedJwksVerifier(
        users.jwks, issuer=settings.jwt_issuer, audience=settings.jwt_audience
    )
    app = create_service_app(
        service_name=SERVICE_NAME,
        title="SportApp - BFF web",
        api_version=API_VERSION,
        settings=settings,
        routers=[
            build_registration_router(users),
            build_sessions_router(
                users,
                allowed_origins=settings.cors_allowed_origins,
                cookie=RefreshCookie(same_site=settings.refresh_cookie_same_site),
            ),
            build_capabilities_router(
                verify_token=verifier.verify,
                permissions=permissions,
                athlete_snapshot=affiliation.user_capabilities,
                channel=CHANNEL,
            ),
            build_profile_router(verify_token=verifier.verify, users=users),
            build_training_router(
                verify_token=verifier.verify,
                permissions=permissions,
                users=users,
                training=training,
                channel=CHANNEL,
            ),
            build_nutrition_router(
                verify_token=verifier.verify,
                permissions=permissions,
                users=users,
                training=training,
                channel=CHANNEL,
            ),
            build_training_routes_router(
                verify_token=verifier.verify,
                permissions=permissions,
                training=training,
                channel=CHANNEL,
            ),
            build_training_plan_templates_router(
                verify_token=verifier.verify,
                permissions=permissions,
                training=training,
                channel=CHANNEL,
            ),
            build_training_history_router(
                verify_token=verifier.verify,
                permissions=permissions,
                training=training,
                users=users,
                channel=CHANNEL,
            ),
            build_services_router(
                verify_token=verifier.verify,
                permissions=permissions,
                services=ComplementaryServicesClient(services_http),
                channel=CHANNEL,
            ),
            build_service_availability_router(
                verify_token=verifier.verify,
                services=ComplementaryServicesClient(services_http),
            ),
            build_service_associations_router(
                verify_token=verifier.verify,
                affiliation=affiliation,
                services=ComplementaryServicesClient(services_http),
            ),
            build_events_router(
                verify_token=verifier.verify,
                permissions=permissions,
                events=events,
                channel=CHANNEL,
            ),
            build_event_publication_router(
                verify_token=verifier.verify,
                permissions=permissions,
                events=events,
                channel=CHANNEL,
            ),
            build_event_attendees_router(
                verify_token=verifier.verify,
                permissions=permissions,
                events=events,
                users=users,
                channel=CHANNEL,
            ),
            build_event_calendar_router(
                verify_token=verifier.verify,
                permissions=permissions,
                events=events,
                channel=CHANNEL,
            ),
            build_event_fee_router(
                verify_token=verifier.verify,
                permissions=permissions,
                events=events,
                channel=CHANNEL,
            ),
            build_subscriptions_router(verify_token=verifier.verify, affiliation=affiliation),
            build_integrated_calendar_router(
                verify_token=verifier.verify,
                permissions=permissions,
                athlete_snapshot=affiliation.user_capabilities,
                events=events,
                services=ComplementaryServicesClient(services_http),
                training=training,
                channel=CHANNEL,
                source_timeout=settings.upstream_timeout_seconds,
            ),
        ],
        shutdown_hooks=[
            users_http.close,
            affiliation_http.close,
            training_http.close,
            services_http.close,
            events_http.close,
        ],
    )
    # Se agrega al final para que sea la capa externa y responda la preflight.
    # Con credenciales (HU003 0.4.0): el navegador envía la cookie del refresh token, por
    # eso solo se admiten orígenes explícitos, nunca `*`.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_allowed_origins),
        allow_methods=list(CORS_ALLOWED_METHODS),
        allow_headers=list(CORS_ALLOWED_HEADERS),
        expose_headers=list(CORS_EXPOSED_HEADERS),
        allow_credentials=True,
    )
    return app
