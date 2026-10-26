"""Cliente HTTP de `complementary_services` según su OpenAPI 0.6.0 (ADR-038, ADR-046).

HU029 y HU040 consultan `GET /v1/services/availability`; HU040 además reserva, cancela y lista
las citas propias (`/v1/me/appointments`). Reenvía el access token del deportista (el servicio
vuelve a verificar identidad, actor y capacidad), la clave de idempotencia y la correlación;
cuerpo y parámetros viajan tal cual: los valida el servicio.
"""

from typing import Any
from uuid import UUID

import httpx

from bff_mobile.clients.users_management import UpstreamTimeout, UpstreamUnavailable
from sportapp_platform.correlation import CORRELATION_HEADER, current_correlation_id

IDEMPOTENCY_HEADER = "Idempotency-Key"
APPOINTMENTS_PATH = "/v1/me/appointments"


class ComplementaryServicesClient:
    def __init__(self, http: httpx.Client) -> None:
        self._http = http

    def service_availability(
        self, access_token: str, params: list[tuple[str, str]]
    ) -> httpx.Response:
        """Una página de servicios publicados con sus intervalos; `category` es repetible."""
        return self._send("GET", "/v1/services/availability", access_token, params=tuple(params))

    def book_appointment(
        self, access_token: str, body: Any, idempotency_key: str | None
    ) -> httpx.Response:
        """HU040 (ADR-046): el cuerpo y la clave viajan tal cual; el servicio responde la
        elegibilidad, el cupo, la unicidad y los errores de validación."""
        return self._send(
            "POST", APPOINTMENTS_PATH, access_token, json=body, idempotency_key=idempotency_key
        )

    def cancel_appointment(self, access_token: str, appointment_id: UUID) -> httpx.Response:
        """HU040: cancelación de una cita propia (repetirla devuelve la cita sin cambios)."""
        return self._send("DELETE", f"{APPOINTMENTS_PATH}/{appointment_id}", access_token)

    def list_my_appointments(self, access_token: str, params: dict[str, str]) -> httpx.Response:
        """HU040: citas propias; los parámetros los valida el servicio."""
        return self._send("GET", APPOINTMENTS_PATH, access_token, params=params)

    def _send(
        self,
        method: str,
        path: str,
        access_token: str,
        *,
        json: Any = None,
        idempotency_key: str | None = None,
        params: dict[str, str] | tuple[tuple[str, str], ...] | None = None,
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
