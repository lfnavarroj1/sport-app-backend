"""Aplicación FastAPI de `bff_mobile`."""

import httpx
from fastapi import FastAPI

from bff_mobile.api.appointments import build_appointments_router
from bff_mobile.api.calendar import build_calendar_router
from bff_mobile.api.capabilities import build_capabilities_router
from bff_mobile.api.devices import build_devices_router
from bff_mobile.api.event_registrations import build_event_registrations_router
from bff_mobile.api.event_suggestions import build_event_suggestions_router
from bff_mobile.api.notifications import build_notifications_router
from bff_mobile.api.route_suggestions import (
    AthleteContextCache,
    build_route_suggestions_router,
)
from bff_mobile.api.service_availability import build_service_availability_router
from bff_mobile.api.service_suggestions import build_service_suggestions_router
from bff_mobile.api.session_reminders import build_session_reminders_router
from bff_mobile.api.sessions import build_sessions_router
from bff_mobile.api.template_suggestions import build_template_suggestions_router
from bff_mobile.api.training_dashboard import build_training_dashboard_router
from bff_mobile.api.training_metrics import build_training_metrics_router
from bff_mobile.api.training_sessions import build_training_sessions_router
from bff_mobile.clients.affiliation_management import AffiliationManagementClient
from bff_mobile.clients.complementary_services import ComplementaryServicesClient
from bff_mobile.clients.events_management import EventsManagementClient
from bff_mobile.clients.training_management import TrainingManagementClient
from bff_mobile.clients.users_management import UsersManagementClient
from bff_mobile.settings import BffMobileSettings
from sportapp_platform.app import create_service_app
from sportapp_platform.authorization import load_actor_permissions, resolve_actor_permissions_path
from sportapp_platform.config import load_settings
from sportapp_platform.tokens import CachedJwksVerifier

SERVICE_NAME = "bff-mobile"
API_VERSION = "0.18.0"
CHANNEL = "mobile"


def create_app(
    settings: BffMobileSettings | None = None,
    users_http: httpx.Client | None = None,
    affiliation_http: httpx.Client | None = None,
    training_http: httpx.Client | None = None,
    events_http: httpx.Client | None = None,
    *,
    services_http: httpx.Client | None = None,
) -> FastAPI:
    """Los transportes `*_http` permiten inyectar servicios en pruebas de componente."""
    settings = settings or load_settings(BffMobileSettings)
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
    events_http = events_http or httpx.Client(
        base_url=str(settings.events_management_base_url),
        timeout=settings.upstream_timeout_seconds,
    )
    services_http = services_http or httpx.Client(
        base_url=str(settings.complementary_services_base_url),
        timeout=settings.upstream_timeout_seconds,
    )
    users = UsersManagementClient(users_http)
    training = TrainingManagementClient(training_http)
    events = EventsManagementClient(events_http)
    affiliation = AffiliationManagementClient(affiliation_http)
    services = ComplementaryServicesClient(services_http)
    # Una sola caché del contexto del deportista para todas las sugerencias (ADR-041).
    contexts = AthleteContextCache()
    verifier = CachedJwksVerifier(
        users.jwks, issuer=settings.jwt_issuer, audience=settings.jwt_audience
    )
    return create_service_app(
        service_name=SERVICE_NAME,
        title="SportApp - BFF móvil",
        api_version=API_VERSION,
        settings=settings,
        routers=[
            build_sessions_router(users),
            build_capabilities_router(
                verify_token=verifier.verify,
                permissions=permissions,
                athlete_snapshot=affiliation.user_capabilities,
                channel=CHANNEL,
            ),
            build_training_sessions_router(verify_token=verifier.verify, training=training),
            build_calendar_router(
                verify_token=verifier.verify,
                athlete_snapshot=affiliation.user_capabilities,
                events=events,
                training=training,
            ),
            build_event_registrations_router(verify_token=verifier.verify, events=events),
            build_notifications_router(verify_token=verifier.verify, events=events),
            build_training_metrics_router(
                verify_token=verifier.verify,
                athlete_snapshot=affiliation.user_capabilities,
                training=training,
            ),
            build_training_dashboard_router(
                verify_token=verifier.verify,
                athlete_snapshot=affiliation.user_capabilities,
                training=training,
            ),
            build_route_suggestions_router(
                verify_token=verifier.verify,
                athlete_snapshot=affiliation.user_capabilities,
                users=users,
                training=training,
                cache=contexts,
            ),
            build_template_suggestions_router(
                verify_token=verifier.verify,
                athlete_snapshot=affiliation.user_capabilities,
                users=users,
                training=training,
                cache=contexts,
            ),
            build_event_suggestions_router(
                verify_token=verifier.verify,
                athlete_snapshot=affiliation.user_capabilities,
                users=users,
                events=events,
                cache=contexts,
            ),
            build_service_suggestions_router(
                verify_token=verifier.verify,
                athlete_snapshot=affiliation.user_capabilities,
                users=users,
                events=events,
                services=services,
            ),
            build_devices_router(
                verify_token=verifier.verify,
                athlete_snapshot=affiliation.user_capabilities,
                training=training,
            ),
            build_session_reminders_router(
                verify_token=verifier.verify,
                athlete_snapshot=affiliation.user_capabilities,
                training=training,
            ),
            build_service_availability_router(verify_token=verifier.verify, services=services),
            build_appointments_router(
                verify_token=verifier.verify,
                athlete_snapshot=affiliation.user_capabilities,
                services=services,
            ),
        ],
        shutdown_hooks=[
            users_http.close,
            affiliation_http.close,
            training_http.close,
            events_http.close,
            services_http.close,
        ],
    )
