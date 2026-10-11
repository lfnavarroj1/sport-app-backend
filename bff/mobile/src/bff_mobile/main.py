"""Aplicación FastAPI de `bff_mobile`."""

import httpx
from fastapi import FastAPI

from bff_mobile.api.capabilities import build_capabilities_router
from bff_mobile.api.sessions import build_sessions_router
from bff_mobile.clients.affiliation_management import AffiliationManagementClient
from bff_mobile.clients.users_management import UsersManagementClient
from bff_mobile.settings import BffMobileSettings
from sportapp_platform.app import create_service_app
from sportapp_platform.authorization import load_actor_permissions, resolve_actor_permissions_path
from sportapp_platform.config import load_settings
from sportapp_platform.tokens import CachedJwksVerifier

SERVICE_NAME = "bff-mobile"
API_VERSION = "0.3.0"
CHANNEL = "mobile"


def create_app(
    settings: BffMobileSettings | None = None,
    users_http: httpx.Client | None = None,
    affiliation_http: httpx.Client | None = None,
) -> FastAPI:
    """`users_http` y `affiliation_http` permiten inyectar transportes en pruebas de componente."""
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
    users = UsersManagementClient(users_http)
    affiliation = AffiliationManagementClient(affiliation_http)
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
        ],
        shutdown_hooks=[users_http.close, affiliation_http.close],
    )
