"""Aplicación FastAPI de `third_party_management`."""

from fastapi import FastAPI

from sportapp_platform.app import create_service_app
from sportapp_platform.config import load_settings
from third_party_management.settings import ThirdPartyManagementSettings

SERVICE_NAME = "third-party-management"
API_VERSION = "0.1.0"


def create_app(settings: ThirdPartyManagementSettings | None = None) -> FastAPI:
    settings = settings or load_settings(ThirdPartyManagementSettings)
    return create_service_app(
        service_name=SERVICE_NAME,
        title="SportApp - Gestión de terceros",
        api_version=API_VERSION,
        settings=settings,
    )
