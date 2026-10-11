"""Aplicación FastAPI de `bff_web`."""

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from bff_web.api.registration import (
    IDEMPOTENCY_HEADER,
    REPLAYED_HEADER,
    build_registration_router,
)
from bff_web.api.sessions import RETRY_AFTER_HEADER, build_sessions_router
from bff_web.clients.users_management import UsersManagementClient
from bff_web.settings import BffWebSettings
from sportapp_platform.app import create_service_app
from sportapp_platform.config import load_settings
from sportapp_platform.correlation import CORRELATION_HEADER

SERVICE_NAME = "bff-web"
API_VERSION = "0.3.0"

# Lo que el navegador puede enviar y leer en llamadas de otro origen.
CORS_ALLOWED_METHODS = ("GET", "POST")
CORS_ALLOWED_HEADERS = ("Content-Type", IDEMPOTENCY_HEADER, CORRELATION_HEADER)
CORS_EXPOSED_HEADERS = ("Location", REPLAYED_HEADER, RETRY_AFTER_HEADER, CORRELATION_HEADER)


def create_app(
    settings: BffWebSettings | None = None, users_http: httpx.Client | None = None
) -> FastAPI:
    """`users_http` permite inyectar el transporte en pruebas de componente."""
    settings = settings or load_settings(BffWebSettings)
    users_http = users_http or httpx.Client(
        base_url=str(settings.users_management_base_url),
        timeout=settings.upstream_timeout_seconds,
    )
    users = UsersManagementClient(users_http)
    app = create_service_app(
        service_name=SERVICE_NAME,
        title="SportApp - BFF web",
        api_version=API_VERSION,
        settings=settings,
        routers=[build_registration_router(users), build_sessions_router(users)],
        shutdown_hooks=[users_http.close],
    )
    # Se agrega al final para que sea la capa externa y responda la preflight.
    # Sin credenciales: la identidad no viaja en cookies.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_allowed_origins),
        allow_methods=list(CORS_ALLOWED_METHODS),
        allow_headers=list(CORS_ALLOWED_HEADERS),
        expose_headers=list(CORS_EXPOSED_HEADERS),
        allow_credentials=False,
    )
    return app
