"""Cliente HTTP de `complementary_services` según su OpenAPI 0.5.0 (HU020, ADR-038).

HU029 solo consulta `GET /v1/services/availability`: reenvía el access token del deportista
(el servicio vuelve a verificar identidad y actor) y la correlación.
"""

import httpx

from bff_mobile.clients.users_management import UpstreamTimeout, UpstreamUnavailable
from sportapp_platform.correlation import CORRELATION_HEADER, current_correlation_id


class ComplementaryServicesClient:
    def __init__(self, http: httpx.Client) -> None:
        self._http = http

    def service_availability(
        self, access_token: str, params: list[tuple[str, str]]
    ) -> httpx.Response:
        """Una página de servicios publicados con sus intervalos; `category` es repetible."""
        headers = {"Authorization": f"Bearer {access_token}"}
        correlation_id = current_correlation_id()
        if correlation_id:
            headers[CORRELATION_HEADER] = correlation_id
        try:
            return self._http.get(
                "/v1/services/availability", params=tuple(params), headers=headers
            )
        except httpx.TimeoutException:
            raise UpstreamTimeout() from None
        except httpx.TransportError:
            raise UpstreamUnavailable() from None
