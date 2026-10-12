"""HU011 en el canal web: historial de entrenamiento (contrato aprobado el 2026-10-12).

`GET /v1/training-history` devuelve el historial propio del deportista;
`GET /v1/athletes/{athlete_id}/training-history`, el de un deportista que autorizó al
profesional autenticado. El BFF autentica, aplica el filtro de borde por actor o
permiso y reenvía con el token del usuario; el servicio decide la autorización final
(permiso y autorización vigente) y sus errores se entregan sin alterar.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from http import HTTPStatus
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Header, Query, Response
from pydantic import BaseModel

from bff_web.api.training import (
    _UNAUTHORIZED,
    _UPSTREAM,
    ATHLETE,
    Sport,
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

HISTORY_VIEW_PERMISSION = "training.history.view_assigned"
MAX_LIMIT = 100
DEFAULT_LIMIT = 20


class SessionHistoryItem(BaseModel):
    session_id: UUID
    plan_id: UUID | None
    sport: Sport
    started_at: datetime
    ended_at: datetime
    duration_seconds: int


class HistoryFilters(BaseModel):
    sports: list[Sport]


class TrainingHistoryResponse(BaseModel):
    items: list[SessionHistoryItem]
    next_cursor: str | None
    filters: HistoryFilters


@dataclass(frozen=True)
class _HistoryParams:
    date_from: date | None
    date_to: date | None
    sport: str | None
    limit: int
    cursor: str | None

    def query(self) -> dict[str, str]:
        pairs = (
            ("from", self.date_from.isoformat() if self.date_from else None),
            ("to", self.date_to.isoformat() if self.date_to else None),
            ("sport", self.sport),
            ("limit", str(self.limit)),
            ("cursor", self.cursor),
        )
        return {name: value for name, value in pairs if value is not None}


_RESPONSES: dict[int | str, dict[str, Any]] = {
    **_UNAUTHORIZED,
    **_UPSTREAM,
    HTTPStatus.FORBIDDEN.value: {
        "model": ErrorResponse,
        "description": "`permission_required` o `athlete_authorization_required` (HU011 CA3).",
    },
    HTTPStatus.UNPROCESSABLE_ENTITY.value: {
        "model": ErrorResponse,
        "description": "`validation_error`: rango de fechas, límite o cursor inválidos.",
    },
}


def build_training_history_router(
    *,
    verify_token: Callable[[str], VerifiedAccessToken],
    permissions: ActorPermissions,
    training: TrainingManagementClient,
    channel: str,
) -> APIRouter:
    router = APIRouter(tags=["training"])

    def forward(session: _Session, athlete_id: UUID | None, params: _HistoryParams) -> Response:
        try:
            upstream = training.get_history(session.token, athlete_id, params.query())
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
        "/v1/training-history",
        response_model=TrainingHistoryResponse,
        summary="Mi historial de entrenamiento (HU011)",
        responses=_RESPONSES,
    )
    def own_history(
        *,
        date_from: Annotated[date | None, Query(alias="from")] = None,
        date_to: Annotated[date | None, Query(alias="to")] = None,
        sport: Sport | None = None,
        limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
        cursor: Annotated[str | None, Query(max_length=512)] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        session = authenticate_session(verify_token, authorization)
        if isinstance(session, Response):
            return session
        if session.actor_type != ATHLETE:
            return _permission_required()
        return forward(session, None, _HistoryParams(date_from, date_to, sport, limit, cursor))

    @router.get(
        "/v1/athletes/{athlete_id}/training-history",
        response_model=TrainingHistoryResponse,
        summary="Historial de entrenamiento de un deportista que me autorizó (HU011)",
        responses=_RESPONSES,
    )
    def athlete_history(
        *,
        athlete_id: UUID,
        date_from: Annotated[date | None, Query(alias="from")] = None,
        date_to: Annotated[date | None, Query(alias="to")] = None,
        sport: Sport | None = None,
        limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
        cursor: Annotated[str | None, Query(max_length=512)] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        session = authenticate_session(verify_token, authorization)
        if isinstance(session, Response):
            return session
        actor = session.actor_type
        if actor is None or HISTORY_VIEW_PERMISSION not in permissions.for_actor(actor, channel):
            return _permission_required()
        return forward(
            session, athlete_id, _HistoryParams(date_from, date_to, sport, limit, cursor)
        )

    return router
