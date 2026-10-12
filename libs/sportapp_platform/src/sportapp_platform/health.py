"""Endpoints técnicos `/health/live` y `/health/ready`.

- `live`: el proceso atiende solicitudes; no consulta dependencias.
- `ready`: todas las dependencias requeridas para atender tráfico responden.
"""

import logging
from collections.abc import Sequence
from http import HTTPStatus
from typing import Literal, Protocol

from fastapi import APIRouter, Response
from pydantic import BaseModel, Field

_logger = logging.getLogger("sportapp.health")


class ReadinessCheck(Protocol):
    @property
    def name(self) -> str: ...

    def __call__(self) -> bool: ...


class HealthStatus(BaseModel):
    status: Literal["ok", "unavailable"]
    checks: dict[str, Literal["ok", "failed"]] = Field(default_factory=dict)


def _run_check(check: ReadinessCheck) -> bool:
    try:
        return check()
    except Exception:
        _logger.warning("health.check_error", extra={"fields": {"check": check.name}})
        return False


def build_health_router(readiness_checks: Sequence[ReadinessCheck]) -> APIRouter:
    router = APIRouter(prefix="/health", tags=["health"])

    @router.get("/live", response_model=HealthStatus)
    def live() -> HealthStatus:
        return HealthStatus(status="ok")

    @router.get(
        "/ready",
        response_model=HealthStatus,
        responses={HTTPStatus.SERVICE_UNAVAILABLE.value: {"model": HealthStatus}},
    )
    def ready(response: Response) -> HealthStatus:
        results: dict[str, Literal["ok", "failed"]] = {
            check.name: "ok" if _run_check(check) else "failed" for check in readiness_checks
        }
        if all(result == "ok" for result in results.values()):
            return HealthStatus(status="ok", checks=results)
        response.status_code = HTTPStatus.SERVICE_UNAVAILABLE
        return HealthStatus(status="unavailable", checks=results)

    return router
