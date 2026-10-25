"""Destinatarios de HU039 desde el contrato interno de Usuarios (ADR-045 §8).

`POST /v1/users/athletes/search` (users-management 0.15.0) devuelve una página de
`user_ids` de deportistas activos cuyo perfil coincide en deporte, ciudad y país, con
`next_cursor`. Se llama sin token (red interna de la plataforma) y fuera de cualquier
transacción local. Cualquier respuesta distinta de `200` con la forma esperada, un error de
red o un timeout es `RecipientsUnavailable`: el worker no confirma el mensaje y Pub/Sub lo
reintenta (CA4).
"""

from collections.abc import Sequence
from uuid import UUID

import httpx

from events_management.application.ports import AthletePage, RecipientsUnavailable
from sportapp_platform.correlation import CORRELATION_HEADER, current_correlation_id

SEARCH_PATH = "/v1/users/athletes/search"


class UsersAthleteDirectory:
    def __init__(self, http: httpx.Client) -> None:
        self._http = http

    def search(
        self, *, sports: Sequence[str], city: str, country_code: str, cursor: str | None, limit: int
    ) -> AthletePage:
        correlation_id = current_correlation_id()
        headers = {CORRELATION_HEADER: correlation_id} if correlation_id else {}
        body = {
            "sports": list(sports),
            "city": city,
            "country_code": country_code,
            "limit": limit,
            "cursor": cursor,
        }
        try:
            response = self._http.post(SEARCH_PATH, json=body, headers=headers)
        except httpx.HTTPError:
            raise RecipientsUnavailable() from None
        if response.status_code != httpx.codes.OK:
            raise RecipientsUnavailable()
        try:
            document = response.json()
            user_ids = tuple(UUID(str(value)) for value in document["user_ids"])
            next_cursor = document["next_cursor"]
        except (ValueError, KeyError, TypeError):
            raise RecipientsUnavailable() from None
        if next_cursor is not None and not isinstance(next_cursor, str):
            raise RecipientsUnavailable()
        return AthletePage(user_ids=user_ids, next_cursor=next_cursor)
