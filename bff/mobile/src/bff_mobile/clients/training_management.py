"""Cliente HTTP de `training_management` según su OpenAPI 0.5.0 (HU032 y HU035).

Reenvía el access token del usuario, la clave de idempotencia y la correlación; el
servicio vuelve a verificar identidad, actor y capacidad.
"""

from datetime import date
from typing import Any

import httpx

from bff_mobile.clients.users_management import UpstreamTimeout, UpstreamUnavailable
from sportapp_platform.correlation import CORRELATION_HEADER, current_correlation_id

IDEMPOTENCY_HEADER = "Idempotency-Key"


class TrainingManagementClient:
    def __init__(self, http: httpx.Client) -> None:
        self._http = http

    def start_session(
        self, access_token: str, body: Any, idempotency_key: str | None
    ) -> httpx.Response:
        return self._send(
            "POST",
            "/v1/training-sessions",
            access_token,
            body=body,
            idempotency_key=idempotency_key,
        )

    def finish_session(
        self, access_token: str, session_id: str, body: Any, idempotency_key: str | None
    ) -> httpx.Response:
        return self._send(
            "POST",
            f"/v1/training-sessions/{session_id}/finish",
            access_token,
            body=body,
            idempotency_key=idempotency_key,
        )

    def active_session(self, access_token: str) -> httpx.Response:
        return self._send("GET", "/v1/training-sessions/active", access_token)

    def planned_sessions(
        self, access_token: str, date_from: date, date_to: date, limit: int
    ) -> httpx.Response:
        """HU035: sesiones planificadas propias entre dos fechas inclusivas."""
        return self._send(
            "GET",
            "/v1/me/training-plan-sessions",
            access_token,
            params={"from": date_from.isoformat(), "to": date_to.isoformat(), "limit": str(limit)},
        )

    def _send(
        self,
        method: str,
        path: str,
        access_token: str,
        *,
        body: Any = None,
        idempotency_key: str | None = None,
        params: dict[str, str] | None = None,
    ) -> httpx.Response:
        """Cuerpo y clave viajan tal cual: el servicio responde los errores de ADR-029."""
        headers = {"Authorization": f"Bearer {access_token}"}
        if idempotency_key is not None:
            headers[IDEMPOTENCY_HEADER] = idempotency_key
        correlation_id = current_correlation_id()
        if correlation_id:
            headers[CORRELATION_HEADER] = correlation_id
        try:
            if method == "GET":
                return self._http.get(path, headers=headers, params=params)
            return self._http.post(path, json=body, headers=headers)
        except httpx.TimeoutException:
            raise UpstreamTimeout() from None
        except httpx.TransportError:
            raise UpstreamUnavailable() from None
