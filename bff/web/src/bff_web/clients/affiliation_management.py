"""Cliente HTTP del contrato interno de `affiliation_management` (OpenAPI 0.2.0)."""

from typing import Any
from uuid import UUID

import httpx

from bff_web.clients.users_management import UpstreamTimeout, UpstreamUnavailable
from sportapp_platform.correlation import CORRELATION_HEADER, current_correlation_id


class AffiliationManagementClient:
    def __init__(self, http: httpx.Client) -> None:
        self._http = http

    def user_capabilities(self, user_id: UUID) -> dict[str, Any]:
        """Instantánea `ACTIVE`/`NONE` del usuario; cualquier otra respuesta es indisponibilidad."""
        correlation_id = current_correlation_id()
        headers = {CORRELATION_HEADER: correlation_id} if correlation_id else {}
        try:
            response = self._http.get(
                f"/v1/subscriptions/by-user/{user_id}/capabilities", headers=headers
            )
        except httpx.TimeoutException:
            raise UpstreamTimeout() from None
        except httpx.TransportError:
            raise UpstreamUnavailable() from None
        if response.status_code != httpx.codes.OK:
            raise UpstreamUnavailable()
        document: dict[str, Any] = response.json()
        return document
