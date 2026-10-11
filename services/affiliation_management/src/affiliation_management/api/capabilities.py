"""Contrato interno de ADR-024: `GET /v1/subscriptions/by-user/{user_id}/capabilities`.

Solo lo consumen `bff-web` y `bff-mobile` dentro de la red de la plataforma; los
clientes usan `GET /v1/me/capabilities` del BFF, que toma el usuario del token.
"""

from datetime import datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter
from pydantic import BaseModel, Field

from affiliation_management.application.queries.user_capabilities import (
    GetUserCapabilities,
    UserCapabilities,
)


class UserCapabilitiesResponse(BaseModel):
    user_id: UUID
    subscription_status: Literal["ACTIVE", "NONE"] = Field(
        description="`NONE`: sin suscripción activa; nunca se concede `FREE` por suposición."
    )
    plan_code: str | None
    plan_version: int | None
    catalog_version: str | None
    capabilities: list[str]
    evaluated_at: datetime

    @classmethod
    def from_result(cls, result: UserCapabilities) -> "UserCapabilitiesResponse":
        return cls(
            user_id=result.user_id,
            subscription_status=result.subscription_status.value,
            plan_code=result.plan_code,
            plan_version=result.plan_version,
            catalog_version=result.catalog_version,
            capabilities=list(result.capabilities),
            evaluated_at=result.evaluated_at,
        )


def build_capabilities_router(get_capabilities: GetUserCapabilities) -> APIRouter:
    router = APIRouter(prefix="/v1/subscriptions", tags=["subscriptions"])

    @router.get(
        "/by-user/{user_id}/capabilities",
        response_model=UserCapabilitiesResponse,
        summary="Capacidades vigentes de un usuario (interno, HU001/ADR-024)",
    )
    def get_user_capabilities(user_id: UUID) -> UserCapabilitiesResponse:
        return UserCapabilitiesResponse.from_result(get_capabilities(user_id))

    return router
