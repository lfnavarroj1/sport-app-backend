"""Cliente HTTP de `users_management` según su OpenAPI 0.6.0."""

from typing import Any
from uuid import UUID

import httpx

from sportapp_platform.correlation import CORRELATION_HEADER, current_correlation_id

IDEMPOTENCY_HEADER = "Idempotency-Key"
CLIENT_IP_HEADER = "X-Client-IP"


class UpstreamUnavailable(Exception):
    code = "upstream_unavailable"


class UpstreamTimeout(Exception):
    code = "upstream_timeout"


class UsersManagementClient:
    def __init__(self, http: httpx.Client) -> None:
        self._http = http

    def register_user(self, payload: dict[str, Any], idempotency_key: UUID) -> httpx.Response:
        return self._post("/v1/users", payload, {IDEMPOTENCY_HEADER: str(idempotency_key)})

    def create_session(self, payload: dict[str, Any], client_ip: str | None) -> httpx.Response:
        # La IP del usuario alimenta el control de abuso por correo + IP del servicio.
        headers = {CLIENT_IP_HEADER: client_ip} if client_ip else {}
        return self._post("/v1/sessions", payload, headers)

    def refresh_session(self, payload: dict[str, Any]) -> httpx.Response:
        return self._post("/v1/sessions/refresh", payload, {})

    def revoke_session(self, payload: dict[str, Any]) -> httpx.Response:
        """HU004: revoca la sesión del refresh token; el servicio responde `204` siempre."""
        return self._post("/v1/sessions/revoke", payload, {})

    def lookup_user(self, email: str) -> dict[str, Any] | None:
        """HU010: `user_id` y `actor_type` de una cuenta activa con ese correo exacto.

        `None` si no existe; cualquier otra respuesta inesperada es indisponibilidad.
        """
        response = self._post("/v1/users/lookup", {"email": email}, {})
        if response.status_code == httpx.codes.NOT_FOUND:
            return None
        if response.status_code != httpx.codes.OK:
            raise UpstreamUnavailable()
        document: dict[str, Any] = response.json()
        return document

    def put_demographic_profile(
        self, access_token: str, body: bytes, idempotency_key: str | None
    ) -> httpx.Response:
        """HU005 (ADR-034): bytes del cuerpo y clave tal cual; el servicio valida y responde
        el `422`, también ante un JSON mal formado."""
        return self._send_as_user(
            "PUT",
            "/v1/me/demographic-profile",
            access_token,
            content=body,
            idempotency_key=idempotency_key,
        )

    def put_physiological_profile(
        self, access_token: str, body: bytes, idempotency_key: str | None
    ) -> httpx.Response:
        """HU005 (ADR-034): bytes del cuerpo y clave tal cual; el servicio valida y responde
        el `422`, también ante un JSON mal formado."""
        return self._send_as_user(
            "PUT",
            "/v1/me/physiological-profile",
            access_token,
            content=body,
            idempotency_key=idempotency_key,
        )

    def put_sport_habits(
        self, access_token: str, body: bytes, idempotency_key: str | None
    ) -> httpx.Response:
        """HU006 (ADR-034): bytes del cuerpo y clave tal cual; el servicio valida y responde
        el `422`, también ante un JSON mal formado."""
        return self._send_as_user(
            "PUT",
            "/v1/me/sport-habits",
            access_token,
            content=body,
            idempotency_key=idempotency_key,
        )

    def get_profile(self, access_token: str) -> httpx.Response:
        """HU005 (ADR-034): perfil proyectado del deportista del token."""
        return self._send_as_user("GET", "/v1/me/profile", access_token)

    def lookup_users_batch(self, user_ids: list[UUID]) -> list[dict[str, Any]]:
        """HU011 (CA5): nombre y correo de cuentas activas por identificador (contrato interno).

        Los identificadores salen siempre de la página autorizada de Entrenamientos. Cualquier
        respuesta distinta de `200` es indisponibilidad.
        """
        response = self._post(
            "/v1/users/lookup-batch", {"user_ids": [str(user_id) for user_id in user_ids]}, {}
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

    def _send_as_user(
        self,
        method: str,
        path: str,
        access_token: str,
        *,
        content: bytes | None = None,
        idempotency_key: str | None = None,
    ) -> httpx.Response:
        # Reenvía el access token: el servicio vuelve a verificar identidad y actor.
        headers = {"Authorization": f"Bearer {access_token}"}
        if content:
            headers["Content-Type"] = "application/json"
        if idempotency_key is not None:
            headers[IDEMPOTENCY_HEADER] = idempotency_key
        correlation_id = current_correlation_id()
        if correlation_id:
            headers[CORRELATION_HEADER] = correlation_id
        try:
            return self._http.request(method, path, content=content, headers=headers)
        except httpx.TimeoutException:
            raise UpstreamTimeout() from None
        except httpx.TransportError:
            raise UpstreamUnavailable() from None
