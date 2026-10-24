"""Cliente HTTP de `users_management` (operaciones del canal móvil y contratos internos de
HU026: contexto del deportista y nombres del directorio)."""

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

    def athlete_context(self, user_id: UUID, *, timeout: float) -> dict[str, Any] | None:
        """HU026 (ADR-041): contexto interno del deportista, sin token. `None` si no hay una
        cuenta activa (`404`); cualquier otra respuesta distinta de `200` es indisponibilidad."""
        headers = {}
        correlation_id = current_correlation_id()
        if correlation_id:
            headers[CORRELATION_HEADER] = correlation_id
        try:
            response = self._http.get(
                f"/v1/users/{user_id}/athlete-context", headers=headers, timeout=timeout
            )
        except httpx.TimeoutException:
            raise UpstreamTimeout() from None
        except httpx.TransportError:
            raise UpstreamUnavailable() from None
        if response.status_code == httpx.codes.NOT_FOUND:
            return None
        if response.status_code != httpx.codes.OK:
            raise UpstreamUnavailable()
        document: dict[str, Any] = response.json()
        return document

    def directory_lookup(self, user_ids: list[UUID]) -> list[dict[str, Any]]:
        """ADR-039: solo el nombre (`full_name`) de cuentas activas (contrato interno, ≤ 100).
        En HU026, los organizadores de las rutas sugeridas. Cualquier respuesta distinta de
        `200` es indisponibilidad."""
        response = self._post(
            "/v1/users/directory-lookup", {"user_ids": [str(user_id) for user_id in user_ids]}, {}
        )
        if response.status_code != httpx.codes.OK:
            raise UpstreamUnavailable()
        items: list[dict[str, Any]] = response.json()["items"]
        return items

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
