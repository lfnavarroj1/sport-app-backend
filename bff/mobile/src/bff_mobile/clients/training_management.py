"""Cliente HTTP de `training_management` según su OpenAPI (HU026, HU028, HU032-HU038).

Reenvía el access token del usuario, la clave de idempotencia, `If-None-Match` y la
correlación; el servicio vuelve a verificar identidad, actor y capacidad. Los segmentos de
ruta recibidos del cliente (`session_id`, `device_id`) se codifican completos, sin validarlos:
el servicio decide su formato (ADR-044).
"""

from datetime import date
from typing import Any
from urllib.parse import quote

import httpx

from bff_mobile.clients.users_management import UpstreamTimeout, UpstreamUnavailable
from sportapp_platform.correlation import CORRELATION_HEADER, current_correlation_id

IDEMPOTENCY_HEADER = "Idempotency-Key"
IF_NONE_MATCH_HEADER = "If-None-Match"


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

    def metrics_catalog(self, access_token: str, if_none_match: str | None) -> httpx.Response:
        """HU034: catálogo de métodos; con `If-None-Match` vigente el servicio responde `304`."""
        return self._send(
            "GET", "/v1/training-metrics/catalog", access_token, if_none_match=if_none_match
        )

    def calculate_metric(
        self, access_token: str, body: Any, idempotency_key: str | None
    ) -> httpx.Response:
        """HU034: calcular un indicador; cuerpo y clave viajan tal cual (ADR-030)."""
        return self._send(
            "POST",
            "/v1/training-metrics/calculations",
            access_token,
            body=body,
            idempotency_key=idempotency_key,
        )

    def metric_calculations(self, access_token: str, params: dict[str, str]) -> httpx.Response:
        """HU034: resultados propios proyectados; solo los parámetros recibidos."""
        return self._send("GET", "/v1/training-metrics/calculations", access_token, params=params)

    def training_dashboard(self, access_token: str, params: dict[str, str]) -> httpx.Response:
        """HU033: tablero propio; `from`/`to` tal como llegaron."""
        return self._send("GET", "/v1/me/training-dashboard", access_token, params=params)

    def route_suggestions(self, access_token: str, params: list[tuple[str, str]]) -> httpx.Response:
        """HU026: rutas sugeridas con el contexto del deportista como filtros explícitos
        (`sport` repetible, `city`, `country_code`) y el `limit` recibido."""
        return self._send(
            "GET",
            "/v1/me/suggestions/routes",
            access_token,
            params=httpx.QueryParams(tuple(params)),
        )

    def training_plan_template_suggestions(
        self, access_token: str, params: list[tuple[str, str]]
    ) -> httpx.Response:
        """HU028: plantillas sugeridas con el contexto del deportista como filtros explícitos
        (`sport` repetible y `primary_goal`) y el `limit` recibido."""
        return self._send(
            "GET",
            "/v1/me/suggestions/training-plan-templates",
            access_token,
            params=httpx.QueryParams(tuple(params)),
        )

    def register_device(
        self, access_token: str, body: Any, idempotency_key: str | None
    ) -> httpx.Response:
        """HU037: vincular o reactivar un dispositivo; cuerpo y clave viajan tal cual."""
        return self._send(
            "POST", "/v1/me/devices", access_token, body=body, idempotency_key=idempotency_key
        )

    def list_devices(self, access_token: str) -> httpx.Response:
        """HU037: dispositivos propios, activos y revocados."""
        return self._send("GET", "/v1/me/devices", access_token)

    def revoke_device(self, access_token: str, device_id: str) -> httpx.Response:
        """HU037: revocar un dispositivo propio; sin cuerpo ni clave."""
        return self._send("DELETE", f"/v1/me/devices/{_segment(device_id)}", access_token)

    def ingest_measurements(self, access_token: str, session_id: str, body: Any) -> httpx.Response:
        """HU037: lote de mediciones de una sesión propia; sin clave (idempotencia natural)."""
        return self._send(
            "POST",
            f"/v1/training-sessions/{_segment(session_id)}/measurements",
            access_token,
            body=body,
        )

    def monitoring_settings(self, access_token: str) -> httpx.Response:
        """HU036: umbral de alerta vigente del deportista del token."""
        return self._send("GET", "/v1/me/monitoring-settings", access_token)

    def update_monitoring_settings(self, access_token: str, body: Any) -> httpx.Response:
        """HU036: reemplazar el umbral; el cuerpo viaja tal cual (PUT idempotente)."""
        return self._send("PUT", "/v1/me/monitoring-settings", access_token, body=body)

    def session_reminder_rules(self, access_token: str) -> httpx.Response:
        """HU038: ruleset versionado de recordatorios durante la sesión."""
        return self._send("GET", "/v1/session-reminder-rules", access_token)

    def session_reminder_preferences(self, access_token: str) -> httpx.Response:
        """HU038: preferencias de recordatorios del deportista del token."""
        return self._send("GET", "/v1/me/session-reminder-preferences", access_token)

    def update_session_reminder_preferences(self, access_token: str, body: Any) -> httpx.Response:
        """HU038: reemplazar las preferencias; el cuerpo (con `expected_version`) viaja tal
        cual."""
        return self._send("PUT", "/v1/me/session-reminder-preferences", access_token, body=body)

    def _send(
        self,
        method: str,
        path: str,
        access_token: str,
        *,
        body: Any = None,
        idempotency_key: str | None = None,
        params: dict[str, str] | httpx.QueryParams | None = None,
        if_none_match: str | None = None,
    ) -> httpx.Response:
        """Cuerpo y clave viajan tal cual: el servicio responde los errores de ADR-029/030."""
        headers = {"Authorization": f"Bearer {access_token}"}
        if idempotency_key is not None:
            headers[IDEMPOTENCY_HEADER] = idempotency_key
        if if_none_match is not None:
            headers[IF_NONE_MATCH_HEADER] = if_none_match
        correlation_id = current_correlation_id()
        if correlation_id:
            headers[CORRELATION_HEADER] = correlation_id
        try:
            if method == "GET":
                return self._http.get(path, headers=headers, params=params)
            if method == "DELETE":
                return self._http.delete(path, headers=headers)
            if method == "PUT":
                return self._http.put(path, json=body, headers=headers)
            return self._http.post(path, json=body, headers=headers)
        except httpx.TimeoutException:
            raise UpstreamTimeout() from None
        except httpx.TransportError:
            raise UpstreamUnavailable() from None


def _segment(value: str) -> str:
    """Un segmento de ruta tal como llegó, sin que `/`, `?` o `#` cambien la ruta reenviada."""
    return quote(value, safe="")
