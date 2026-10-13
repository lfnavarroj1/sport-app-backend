"""Cliente HTTP de `events_management` según su OpenAPI 0.2.0 (HU021).

Reenvía el access token del usuario: el servicio vuelve a verificar identidad y permiso.
"""

from typing import Any

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
        headers = {"Authorization": f"Bearer {access_token}"}
        if idempotency_key is not None:
            headers[IDEMPOTENCY_HEADER] = idempotency_key
        correlation_id = current_correlation_id()
        if correlation_id:
            headers[CORRELATION_HEADER] = correlation_id
        try:
            return self._http.post("/v1/events", json=body, headers=headers)
        except httpx.TimeoutException:
            raise UpstreamTimeout() from None
        except httpx.TransportError:
            raise UpstreamUnavailable() from None
