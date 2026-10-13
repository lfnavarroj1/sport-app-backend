"""Cliente HTTP de `events_management` según su OpenAPI 0.3.0 (HU035).

Reenvía el access token del usuario: el servicio vuelve a verificar identidad y la
capacidad `calendar.sport.view` ante Afiliación.
"""

from datetime import datetime

import httpx

from bff_mobile.clients.users_management import UpstreamTimeout, UpstreamUnavailable
from sportapp_platform.correlation import CORRELATION_HEADER, current_correlation_id


class EventsManagementClient:
    def __init__(self, http: httpx.Client) -> None:
        self._http = http

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
