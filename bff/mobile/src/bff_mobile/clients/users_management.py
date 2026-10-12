"""Cliente HTTP de `users_management` según su OpenAPI 0.3.0 (operaciones del canal móvil)."""

from typing import Any
from uuid import UUID

import httpx

from sportapp_platform.correlation import CORRELATION_HEADER, current_correlation_id

CLIENT_IP_HEADER = "X-Client-IP"


class UpstreamUnavailable(Exception):
    code = "upstream_unavailable"


class UpstreamTimeout(Exception):
    code = "upstream_timeout"


class UsersManagementClient:
    def __init__(self, http: httpx.Client) -> None:
        self._http = http

    def create_session(self, payload: dict[str, Any], client_ip: str | None) -> httpx.Response:
        # La IP del usuario alimenta el control de abuso por correo + IP del servicio.
        headers = {CLIENT_IP_HEADER: client_ip} if client_ip else {}
        return self._post("/v1/sessions", payload, headers)

    def refresh_session(self, payload: dict[str, Any]) -> httpx.Response:
        return self._post("/v1/sessions/refresh", payload, {})

    def revoke_session(
        self, payload: dict[str, Any], idempotency_key: UUID | None = None
    ) -> httpx.Response:
        """HU004: revoca la sesión del refresh token; el servicio responde `204` siempre."""
        headers = {"Idempotency-Key": str(idempotency_key)} if idempotency_key else {}
        return self._post("/v1/sessions/revoke", payload, headers)

    def jwks(self) -> dict[str, Any]:
        """Claves públicas de firma; un fallo se propaga para conservar el JWKS anterior."""
        response = self._http.get("/.well-known/jwks.json")
        response.raise_for_status()
        document: dict[str, Any] = response.json()
        return document

    def _post(self, path: str, payload: dict[str, Any], headers: dict[str, str]) -> httpx.Response:
        correlation_id = current_correlation_id()
        if correlation_id:
            headers = {**headers, CORRELATION_HEADER: correlation_id}
        try:
            return self._http.post(path, json=payload, headers=headers)
        except httpx.TimeoutException:
            raise UpstreamTimeout() from None
        except httpx.TransportError:
            raise UpstreamUnavailable() from None
