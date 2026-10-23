"""Cliente HTTP de `affiliation_management` (OpenAPI 0.3.0).

- Contrato interno de capacidades (ADR-024), sin token.
- HU016 (ADR-036): catálogo, suscripción y cambio de plan; reenvía el access token del
  deportista, que Afiliación vuelve a verificar.
"""

from typing import Any
from uuid import UUID

import httpx

from bff_web.clients.users_management import UpstreamTimeout, UpstreamUnavailable
from sportapp_platform.correlation import CORRELATION_HEADER, current_correlation_id

IDEMPOTENCY_HEADER = "Idempotency-Key"


class AffiliationManagementClient:
    def __init__(self, http: httpx.Client) -> None:
        self._http = http

    def list_plans(self, access_token: str) -> httpx.Response:
        return self._send_as_user("GET", "/v1/plans", access_token)

    def my_subscription(self, access_token: str) -> httpx.Response:
        return self._send_as_user("GET", "/v1/me/subscription", access_token)

    def change_subscription(
        self, access_token: str, body: Any, idempotency_key: str | None
    ) -> httpx.Response:
        """El cuerpo y la clave viajan tal cual: Afiliación decide reglas y errores."""
        return self._send_as_user(
            "POST",
            "/v1/me/subscription/changes",
            access_token,
            json=body,
            idempotency_key=idempotency_key,
        )

    def _send_as_user(
        self,
        method: str,
        path: str,
        access_token: str,
        *,
        json: Any = None,
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
                return self._http.request(method, path, headers=headers)
            return self._http.request(method, path, json=json, headers=headers)
        except httpx.TimeoutException:
            raise UpstreamTimeout() from None
        except httpx.TransportError:
            raise UpstreamUnavailable() from None

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
