"""HU012 en el canal web: historial de molestias, incapacidades o lesiones (ADR-037).

`GET /v1/me/health-reports` devuelve los reportes propios del deportista;
`GET /v1/athletes/{athlete_id}/health-reports`, los de un deportista que autorizó al
profesional autenticado. El BFF autentica, aplica el filtro de borde por actor o permiso
(`health.history.view_assigned`) y reenvía con el token del usuario; el servicio decide la
autorización final (permiso y autorización vigente), audita el acceso y sus errores se entregan
sin alterar. El BFF no registra ningún dato de los reportes.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from http import HTTPStatus
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Header, Query, Response
from pydantic import BaseModel, Field

from bff_web.api.training import (
    _UNAUTHORIZED,
    _UPSTREAM,
    ATHLETE,
    _passthrough,
    _permission_required,
    _Session,
    _upstream_failure,
    authenticate_session,
)
from bff_web.clients.training_management import TrainingManagementClient
from bff_web.clients.users_management import UpstreamTimeout, UpstreamUnavailable
from sportapp_platform.authorization import ActorPermissions
from sportapp_platform.errors import ErrorResponse
from sportapp_platform.tokens import VerifiedAccessToken

HEALTH_HISTORY_VIEW_PERMISSION = "health.history.view_assigned"
MAX_LIMIT = 100
DEFAULT_LIMIT = 20


class HealthReportType(StrEnum):
    DISCOMFORT = "DISCOMFORT"
    INJURY = "INJURY"
    INCAPACITY = "INCAPACITY"


class HealthBodyArea(StrEnum):
    HEAD = "HEAD"
    NECK = "NECK"
    SHOULDER = "SHOULDER"
    ARM = "ARM"
    ELBOW = "ELBOW"
    WRIST_HAND = "WRIST_HAND"
    CHEST = "CHEST"
    BACK = "BACK"
    HIP = "HIP"
    THIGH = "THIGH"
    KNEE = "KNEE"
    LOWER_LEG = "LOWER_LEG"
    ANKLE_FOOT = "ANKLE_FOOT"
    OTHER = "OTHER"


class HealthSeverity(StrEnum):
    LOW = "LOW"
    MODERATE = "MODERATE"
    HIGH = "HIGH"


class HealthReportStatus(StrEnum):
    OPEN = "OPEN"


class HealthReportHistoryItem(BaseModel):
    report_id: UUID
    type: HealthReportType
    body_area: HealthBodyArea
    severity: HealthSeverity
    occurred_at: datetime = Field(description="Instante UTC con sufijo `Z`.")
    expected_recovery_days: int | None
    status: HealthReportStatus
    session_id: UUID | None


class HealthReportHistoryResponse(BaseModel):
    items: list[HealthReportHistoryItem] = Field(
        description="Reportes en orden `occurred_at` desc, `report_id` desc; vacía si no hay."
    )
    next_cursor: str | None = Field(
        description="Cursor opaco de la página siguiente; `null` si no hay más."
    )


@dataclass(frozen=True)
class _HistoryParams:
    date_from: date | None
    date_to: date | None
    type: str | None
    severity: str | None
    limit: int
    cursor: str | None

    def query(self) -> dict[str, str]:
        pairs = (
            ("from", self.date_from.isoformat() if self.date_from else None),
            ("to", self.date_to.isoformat() if self.date_to else None),
            ("type", self.type),
            ("severity", self.severity),
            ("limit", str(self.limit)),
            ("cursor", self.cursor),
        )
        return {name: value for name, value in pairs if value is not None}


_RESPONSES: dict[int | str, dict[str, Any]] = {
    **_UNAUTHORIZED,
    **_UPSTREAM,
    HTTPStatus.FORBIDDEN.value: {
        "model": ErrorResponse,
        "description": "`permission_required` o `athlete_authorization_required`, sin revelar "
        "si el deportista existe (HU012 CA3).",
    },
    HTTPStatus.UNPROCESSABLE_ENTITY.value: {
        "model": ErrorResponse,
        "description": "`validation_error`: periodo, filtro, límite o cursor inválidos (CA6).",
    },
}

_FROM = Query(alias="from", description="Fecha inicial (UTC) de `occurred_at`, inclusiva.")
_TO = Query(alias="to", description="Fecha final (UTC) de `occurred_at`, inclusiva.")


def build_health_history_router(
    *,
    verify_token: Callable[[str], VerifiedAccessToken],
    permissions: ActorPermissions,
    training: TrainingManagementClient,
    channel: str,
) -> APIRouter:
    router = APIRouter(tags=["health"])

    def can_view_assigned(session: _Session) -> bool:
        actor = session.actor_type
        return actor is not None and HEALTH_HISTORY_VIEW_PERMISSION in permissions.for_actor(
            actor, channel
        )

    def forward(session: _Session, athlete_id: UUID | None, params: _HistoryParams) -> Response:
        try:
            upstream = training.get_health_reports(session.token, athlete_id, params.query())
        except (UpstreamTimeout, UpstreamUnavailable) as error:
            return _upstream_failure(error)
        response = _passthrough(
            upstream,
            {
                HTTPStatus.OK,
                HTTPStatus.FORBIDDEN,
                HTTPStatus.UNPROCESSABLE_ENTITY,
                HTTPStatus.SERVICE_UNAVAILABLE,
            },
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @router.get(
        "/v1/me/health-reports",
        response_model=HealthReportHistoryResponse,
        summary="Mi historial de molestias, incapacidades o lesiones (HU012)",
        responses=_RESPONSES,
    )
    def own_reports(
        *,
        date_from: Annotated[date | None, _FROM] = None,
        date_to: Annotated[date | None, _TO] = None,
        type: HealthReportType | None = None,
        severity: HealthSeverity | None = None,
        limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
        cursor: Annotated[str | None, Query(max_length=512)] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        session = authenticate_session(verify_token, authorization)
        if isinstance(session, Response):
            return session
        if session.actor_type != ATHLETE:
            return _permission_required()
        return forward(
            session, None, _HistoryParams(date_from, date_to, type, severity, limit, cursor)
        )

    @router.get(
        "/v1/athletes/{athlete_id}/health-reports",
        response_model=HealthReportHistoryResponse,
        summary="Historial de salud de un deportista que me autorizó (HU012)",
        responses=_RESPONSES,
    )
    def athlete_reports(
        *,
        athlete_id: UUID,
        date_from: Annotated[date | None, _FROM] = None,
        date_to: Annotated[date | None, _TO] = None,
        type: HealthReportType | None = None,
        severity: HealthSeverity | None = None,
        limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
        cursor: Annotated[str | None, Query(max_length=512)] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        session = authenticate_session(verify_token, authorization)
        if isinstance(session, Response):
            return session
        if not can_view_assigned(session):
            return _permission_required()
        return forward(
            session, athlete_id, _HistoryParams(date_from, date_to, type, severity, limit, cursor)
        )

    return router
