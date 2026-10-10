"""Aplicación FastAPI de `bff_mobile`."""

from fastapi import FastAPI

from bff_mobile.settings import BffMobileSettings
from sportapp_platform.app import create_service_app
from sportapp_platform.config import load_settings

SERVICE_NAME = "bff-mobile"
API_VERSION = "0.1.0"


def create_app(settings: BffMobileSettings | None = None) -> FastAPI:
    settings = settings or load_settings(BffMobileSettings)
    return create_service_app(
        service_name=SERVICE_NAME,
        title="SportApp - BFF móvil",
        api_version=API_VERSION,
        settings=settings,
    )
