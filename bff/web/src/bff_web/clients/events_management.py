"""Cliente HTTP de `events_management` según su OpenAPI 0.5.0 (HU021, HU024).

Reenvía el access token del usuario: el servicio vuelve a verificar identidad, permiso y
propiedad.
"""

from typing import Any
from uuid import UUID

import httpx

from bff_web.clients.users_management import UpstreamTimeout, UpstreamUnavailable
from sportapp_platform.correlation import CORRELATION_HEADER, current_correlation_id

IDEMPOTENCY_HEADER = "Idempotency-Key"


class EventsManagementClient:
    def __init__(self, http: httpx.Client) -> None:
        self._http = http

    def create_event(
        self, access_token: str, body: Any, idempotency_key: str | None
    ) -> httpx.Response:
        """El cuerpo y la clave viajan tal cual: el servicio responde los errores de ADR-028."""
        return self._send(
            "POST", "/v1/events", access_token, json=body, idempotency_key=idempotency_key
        )

    def publish_event(
        self, access_token: str, event_id: UUID, idempotency_key: str | None
    ) -> httpx.Response:
        """HU024: sin cuerpo; la clave viaja tal cual (ADR-033)."""
        return self._send(
            "POST",
            f"/v1/events/{event_id}/publish",
            access_token,
            idempotency_key=idempotency_key,
        )

    def list_my_events(self, access_token: str, params: dict[str, str]) -> httpx.Response:
        """HU024: eventos propios del organizador del token."""
        return self._send("GET", "/v1/me/events", access_token, params=params)

    def get_event(self, access_token: str, event_id: UUID) -> httpx.Response:
        """ADR-033: detalle visible para quien llama (`DRAFT` solo para su organizador)."""
        return self._send("GET", f"/v1/events/{event_id}", access_token)

    def _send(
        self,
        method: str,
        path: str,
        access_token: str,
        *,
        json: Any = None,
        params: dict[str, str] | None = None,
        idempotency_key: str | None = None,
    ) -> httpx.Response:
        headers = {"Authorization": f"Bearer {access_token}"}
        if idempotency_key is not None:
            headers[IDEMPOTENCY_HEADER] = idempotency_key
        correlation_id = current_correlation_id()
        if correlation_id:
            headers[CORRELATION_HEADER] = correlation_id
        try:
            if json is None:
                return self._http.request(method, path, headers=headers, params=params)
            return self._http.request(method, path, json=json, headers=headers, params=params)
        except httpx.TimeoutException:
            raise UpstreamTimeout() from None
        except httpx.TransportError:
            raise UpstreamUnavailable() from None
