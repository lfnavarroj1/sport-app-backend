"""Aplicación FastAPI de `bff_web`."""

import httpx
from fastapi import FastAPI

from bff_web.api.registration import build_registration_router
from bff_web.clients.users_management import UsersManagementClient
from bff_web.settings import BffWebSettings
from sportapp_platform.app import create_service_app
from sportapp_platform.config import load_settings

SERVICE_NAME = "bff-web"
API_VERSION = "0.2.0"


def create_app(
    settings: BffWebSettings | None = None, users_http: httpx.Client | None = None
) -> FastAPI:
    """`users_http` permite inyectar el transporte en pruebas de componente."""
    settings = settings or load_settings(BffWebSettings)
    users_http = users_http or httpx.Client(
        base_url=str(settings.users_management_base_url),
        timeout=settings.upstream_timeout_seconds,
    )
    return create_service_app(
        service_name=SERVICE_NAME,
        title="SportApp - BFF web",
        api_version=API_VERSION,
        settings=settings,
        routers=[build_registration_router(UsersManagementClient(users_http))],
        shutdown_hooks=[users_http.close],
    )
