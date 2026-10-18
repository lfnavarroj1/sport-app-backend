"""Cliente HTTP de `training_management` según su OpenAPI 0.2.0 (HU010).

Reenvía el access token del usuario: el servicio vuelve a verificar identidad,
permiso, capacidad y autorización del deportista.
"""

from typing import Any
from uuid import UUID

import httpx

from bff_web.clients.users_management import UpstreamTimeout, UpstreamUnavailable
from sportapp_platform.correlation import CORRELATION_HEADER, current_correlation_id

IDEMPOTENCY_HEADER = "Idempotency-Key"


class TrainingManagementClient:
    def __init__(self, http: httpx.Client) -> None:
        self._http = http

    def grant_authorization(self, access_token: str, professional_id: UUID) -> httpx.Response:
        return self._send("PUT", f"/v1/training-authorizations/{professional_id}", access_token)

    def revoke_authorization(self, access_token: str, professional_id: UUID) -> httpx.Response:
        return self._send("DELETE", f"/v1/training-authorizations/{professional_id}", access_token)

    def create_plan(
        self, access_token: str, payload: dict[str, Any], idempotency_key: UUID
    ) -> httpx.Response:
        return self._send(
            "POST",
            "/v1/training-plans",
            access_token,
            json=payload,
            headers={IDEMPOTENCY_HEADER: str(idempotency_key)},
        )

    def get_plan(self, access_token: str, plan_id: UUID) -> httpx.Response:
        return self._send("GET", f"/v1/training-plans/{plan_id}", access_token)

    def get_history(
        self, access_token: str, athlete_id: UUID | None, params: dict[str, str]
    ) -> httpx.Response:
        """HU011: historial propio (`athlete_id=None`) o de un deportista que autorizó."""
        path = (
            f"/v1/athletes/{athlete_id}/training-history" if athlete_id else "/v1/training-history"
        )
        return self._send("GET", path, access_token, params=params)

    def get_authorized_athletes(self, access_token: str, params: dict[str, str]) -> httpx.Response:
        """HU011 (CA5): deportistas con autorización vigente hacia el profesional del token."""
        return self._send("GET", "/v1/me/authorized-athletes", access_token, params=params)

    def _send(
        self,
        method: str,
        path: str,
        access_token: str,
        *,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        params: dict[str, str] | None = None,
    ) -> httpx.Response:
        outgoing = {**(headers or {}), "Authorization": f"Bearer {access_token}"}
        correlation_id = current_correlation_id()
        if correlation_id:
            outgoing[CORRELATION_HEADER] = correlation_id
        try:
            return self._http.request(method, path, json=json, headers=outgoing, params=params)
        except httpx.TimeoutException:
            raise UpstreamTimeout() from None
        except httpx.TransportError:
            raise UpstreamUnavailable() from None
