"""Fábrica común de aplicaciones FastAPI (microservicios y BFF)."""

from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI

from sportapp_platform.config import ServiceSettings
from sportapp_platform.correlation import CorrelationMiddleware
from sportapp_platform.errors import install_error_handlers
from sportapp_platform.health import ReadinessCheck, build_health_router
from sportapp_platform.logging import configure_logging


def create_service_app(
    *,
    service_name: str,
    title: str,
    api_version: str,
    settings: ServiceSettings,
    readiness_checks: Sequence[ReadinessCheck] = (),
    routers: Sequence[APIRouter] = (),
    shutdown_hooks: Sequence[Callable[[], None]] = (),
) -> FastAPI:
    """Construye la aplicación con logs, correlación, errores y salud.

    `api_version` es la versión del contrato OpenAPI publicado por el componente.
    """
    configure_logging(
        service=service_name,
        environment=settings.app_env,
        version=settings.service_version,
        level=settings.log_level,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        for hook in shutdown_hooks:
            hook()

    expose_docs = settings.app_env != "production"
    app = FastAPI(
        title=title,
        version=api_version,
        lifespan=lifespan,
        docs_url="/docs" if expose_docs else None,
        redoc_url=None,
        openapi_url="/openapi.json" if expose_docs else None,
    )
    app.add_middleware(CorrelationMiddleware)
    install_error_handlers(app)
    app.include_router(build_health_router(readiness_checks))
    for router in routers:
        app.include_router(router)
    return app
