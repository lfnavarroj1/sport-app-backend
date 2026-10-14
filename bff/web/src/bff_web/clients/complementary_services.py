"""Cliente HTTP de `complementary_services` según su OpenAPI 0.3.0 (HU015 y HU019).

Reenvía el access token del usuario: el servicio vuelve a verificar identidad y permiso.
"""

from typing import Any
from uuid import UUID

import httpx

from bff_web.clients.users_management import UpstreamTimeout, UpstreamUnavailable
from sportapp_platform.correlation import CORRELATION_HEADER, current_correlation_id

IDEMPOTENCY_HEADER = "Idempotency-Key"


class ComplementaryServicesClient:
    def __init__(self, http: httpx.Client) -> None:
        self._http = http

    def create_service(
        self, access_token: str, body: Any, idempotency_key: str | None
    ) -> httpx.Response:
        """El cuerpo y la clave viajan tal cual: el servicio responde los errores de ADR-027."""
        return self._send(
            "POST", "/v1/services", access_token, json=body, idempotency_key=idempotency_key
        )

    def publish_service(
        self, access_token: str, service_id: UUID, idempotency_key: str | None
    ) -> httpx.Response:
        """HU019: sin cuerpo; si falta la clave no se inventa y el servicio responde `422`."""
        return self._send(
            "POST",
            f"/v1/services/{service_id}/publish",
            access_token,
            idempotency_key=idempotency_key,
        )

    def list_my_services(self, access_token: str, params: dict[str, str]) -> httpx.Response:
        """HU019 (ADR-038): servicios propios; los parámetros los valida el servicio."""
        return self._send("GET", "/v1/me/services", access_token, params=params)

    def get_my_service(self, access_token: str, service_id: UUID) -> httpx.Response:
        """HU019 (ADR-038): detalle de un servicio propio (`404` si es ajeno o no existe)."""
        return self._send("GET", f"/v1/me/services/{service_id}", access_token)

    def _send(
        self,
        method: str,
        path: str,
        access_token: str,
        *,
        json: Any = None,
        idempotency_key: str | None = None,
        params: dict[str, str] | None = None,
    ) -> httpx.Response:
        headers = {"Authorization": f"Bearer {access_token}"}
        if idempotency_key is not None:
            headers[IDEMPOTENCY_HEADER] = idempotency_key
        correlation_id = current_correlation_id()
        if correlation_id:
            headers[CORRELATION_HEADER] = correlation_id
        try:
            return self._http.request(method, path, json=json, headers=headers, params=params)
        except httpx.TimeoutException:
            raise UpstreamTimeout() from None
        except httpx.TransportError:
            raise UpstreamUnavailable() from None
