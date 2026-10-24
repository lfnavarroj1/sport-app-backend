"""Cliente HTTP de `events_management` según su OpenAPI 0.4.0 (HU035 y HU041).

Reenvía el access token del usuario: el servicio vuelve a verificar identidad y la
capacidad `calendar.sport.view` ante Afiliación.
"""

from datetime import datetime
from uuid import UUID

import httpx

from bff_mobile.clients.users_management import UpstreamTimeout, UpstreamUnavailable
from sportapp_platform.correlation import CORRELATION_HEADER, current_correlation_id

IDEMPOTENCY_HEADER = "Idempotency-Key"


class EventsManagementClient:
    def __init__(self, http: httpx.Client) -> None:
        self._http = http

    def event_detail(self, access_token: str, event_id: UUID) -> httpx.Response:
        """HU041: evento publicado con la inscripción del deportista."""
        return self._send("GET", f"/v1/events/{event_id}", access_token)

    def register(
        self, access_token: str, event_id: UUID, idempotency_key: str | None
    ) -> httpx.Response:
        """HU041: inscripción sin cuerpo; la clave viaja tal cual."""
        headers = {IDEMPOTENCY_HEADER: idempotency_key} if idempotency_key is not None else {}
        return self._send("POST", f"/v1/events/{event_id}/registrations", access_token, headers)

    def my_registrations(
        self, access_token: str, params: dict[str, str], *, timeout: float | None = None
    ) -> httpx.Response:
        """HU041: inscripciones propias con `status`, `limit` y `cursor` tal como llegaron.
        HU029 fija un `timeout` propio para esta fuente opcional."""
        return self._send(
            "GET", "/v1/me/event-registrations", access_token, params=params, timeout=timeout
        )

    def _send(
        self,
        method: str,
        path: str,
        access_token: str,
        extra_headers: dict[str, str] | None = None,
        *,
        params: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> httpx.Response:
        headers = {"Authorization": f"Bearer {access_token}", **(extra_headers or {})}
        correlation_id = current_correlation_id()
        if correlation_id:
            headers[CORRELATION_HEADER] = correlation_id
        try:
            return self._http.request(
                method,
                path,
                headers=headers,
                params=params,
                timeout=httpx.USE_CLIENT_DEFAULT if timeout is None else timeout,
            )
        except httpx.TimeoutException:
            raise UpstreamTimeout() from None
        except httpx.TransportError:
            raise UpstreamUnavailable() from None

    def published_events(
        self, access_token: str, period_from: datetime, period_to: datetime, limit: int
    ) -> httpx.Response:
        headers = {"Authorization": f"Bearer {access_token}"}
        correlation_id = current_correlation_id()
        if correlation_id:
            headers[CORRELATION_HEADER] = correlation_id
        params = {
            "from": period_from.isoformat(),
            "to": period_to.isoformat(),
            "limit": str(limit),
        }
        try:
            return self._http.get("/v1/events", params=params, headers=headers)
        except httpx.TimeoutException:
            raise UpstreamTimeout() from None
        except httpx.TransportError:
            raise UpstreamUnavailable() from None
