"""Aplicación FastAPI de `bff_mobile`."""

import httpx
from fastapi import FastAPI

from bff_mobile.api.sessions import build_sessions_router
from bff_mobile.clients.users_management import UsersManagementClient
from bff_mobile.settings import BffMobileSettings
from sportapp_platform.app import create_service_app
from sportapp_platform.config import load_settings

SERVICE_NAME = "bff-mobile"
API_VERSION = "0.2.0"


def create_app(
    settings: BffMobileSettings | None = None, users_http: httpx.Client | None = None
) -> FastAPI:
    """`users_http` permite inyectar el transporte en pruebas de componente."""
    settings = settings or load_settings(BffMobileSettings)
    users_http = users_http or httpx.Client(
        base_url=str(settings.users_management_base_url),
        timeout=settings.upstream_timeout_seconds,
    )
    return create_service_app(
        service_name=SERVICE_NAME,
        title="SportApp - BFF móvil",
        api_version=API_VERSION,
        settings=settings,
        routers=[build_sessions_router(UsersManagementClient(users_http))],
        shutdown_hooks=[users_http.close],
    )
