"""Cliente HTTP de `training_management` según su OpenAPI (HU010, HU011, HU013, HU014,
HU026, HU028, HU030 y HU018).

Reenvía el access token del usuario: el servicio vuelve a verificar identidad,
permiso, capacidad y autorización del deportista.
"""

from typing import Any
from uuid import UUID

import httpx

from bff_web.clients.users_management import UpstreamTimeout, UpstreamUnavailable
from sportapp_platform.correlation import CORRELATION_HEADER, current_correlation_id

IDEMPOTENCY_HEADER = "Idempotency-Key"
IF_NONE_MATCH_HEADER = "If-None-Match"


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

    def create_nutrition_plan(
        self, access_token: str, payload: dict[str, Any], idempotency_key: UUID
    ) -> httpx.Response:
        """HU014: crear y asignar un plan de alimentación (ADR-037)."""
        return self._send(
            "POST",
            "/v1/nutrition-plans",
            access_token,
            json=payload,
            headers={IDEMPOTENCY_HEADER: str(idempotency_key)},
        )

    def get_nutrition_plan(self, access_token: str, plan_id: UUID) -> httpx.Response:
        """HU014: detalle de un plan de alimentación (lectura CQRS)."""
        return self._send("GET", f"/v1/nutrition-plans/{plan_id}", access_token)

    def list_my_nutrition_plans(self, access_token: str, params: dict[str, str]) -> httpx.Response:
        """HU014: planes de alimentación propios del deportista del token."""
        return self._send("GET", "/v1/me/nutrition-plans", access_token, params=params)

    def list_food_intakes(
        self, access_token: str, athlete_id: UUID | None, params: dict[str, str]
    ) -> httpx.Response:
        """HU013: consumos propios (`athlete_id=None`) o de un deportista que autorizó."""
        path = f"/v1/athletes/{athlete_id}/food-intakes" if athlete_id else "/v1/me/food-intakes"
        return self._send("GET", path, access_token, params=params)

    def food_catalog(self, access_token: str, if_none_match: str | None) -> httpx.Response:
        """HU014: catálogo cerrado de alimentos con `ETag`; `304` si `If-None-Match` coincide."""
        headers = {IF_NONE_MATCH_HEADER: if_none_match} if if_none_match is not None else None
        return self._send("GET", "/v1/food-catalog", access_token, headers=headers)

    def publish_route(
        self, access_token: str, payload: dict[str, Any], idempotency_key: UUID
    ) -> httpx.Response:
        """HU026: publicar una ruta (ADR-041)."""
        return self._send(
            "POST",
            "/v1/routes",
            access_token,
            json=payload,
            headers={IDEMPOTENCY_HEADER: str(idempotency_key)},
        )

    def retire_route(
        self, access_token: str, route_id: UUID, payload: dict[str, Any]
    ) -> httpx.Response:
        """HU026: retirar una ruta propia con `expected_version`."""
        return self._send("POST", f"/v1/routes/{route_id}/retire", access_token, json=payload)

    def list_my_routes(self, access_token: str, params: dict[str, str]) -> httpx.Response:
        """HU026: rutas del organizador del token (lectura CQRS)."""
        return self._send("GET", "/v1/me/routes", access_token, params=params)

    def publish_training_plan_template(
        self, access_token: str, payload: dict[str, Any], idempotency_key: UUID
    ) -> httpx.Response:
        """HU028: publicar una plantilla de plan (ADR-041)."""
        return self._send(
            "POST",
            "/v1/training-plan-templates",
            access_token,
            json=payload,
            headers={IDEMPOTENCY_HEADER: str(idempotency_key)},
        )

    def retire_training_plan_template(
        self, access_token: str, template_id: UUID, payload: dict[str, Any]
    ) -> httpx.Response:
        """HU028: retirar una plantilla propia con `expected_version`."""
        return self._send(
            "POST", f"/v1/training-plan-templates/{template_id}/retire", access_token, json=payload
        )

    def list_my_training_plan_templates(
        self, access_token: str, params: dict[str, str]
    ) -> httpx.Response:
        """HU028: plantillas del profesional del token (lectura CQRS)."""
        return self._send("GET", "/v1/me/training-plan-templates", access_token, params=params)

    def publish_nutrition_routine_template(
        self, access_token: str, payload: dict[str, Any], idempotency_key: UUID
    ) -> httpx.Response:
        """HU030: publicar una rutina de alimentación sugerible (ADR-041)."""
        return self._send(
            "POST",
            "/v1/nutrition-routine-templates",
            access_token,
            json=payload,
            headers={IDEMPOTENCY_HEADER: str(idempotency_key)},
        )

    def retire_nutrition_routine_template(
        self, access_token: str, template_id: UUID, payload: dict[str, Any]
    ) -> httpx.Response:
        """HU030: retirar una rutina propia con `expected_version`."""
        return self._send(
            "POST",
            f"/v1/nutrition-routine-templates/{template_id}/retire",
            access_token,
            json=payload,
        )

    def list_my_nutrition_routine_templates(
        self, access_token: str, params: dict[str, str]
    ) -> httpx.Response:
        """HU030: rutinas del profesional del token (lectura CQRS)."""
        return self._send("GET", "/v1/me/nutrition-routine-templates", access_token, params=params)

    def planned_sessions(self, access_token: str, params: dict[str, str]) -> httpx.Response:
        """HU018 (ADR-031): sesiones planificadas del deportista del token por fecha."""
        return self._send("GET", "/v1/me/training-plan-sessions", access_token, params=params)

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
