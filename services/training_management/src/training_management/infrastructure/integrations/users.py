"""Restricciones alimenticias del deportista desde el contrato interno de Usuarios (ADR-035).

`GET /v1/users/{user_id}/nutrition-restrictions` (sin token, dentro de la red de la
plataforma, como `POST /v1/users/lookup`) responde `200 {restrictions, allergies,
diet_pattern, version}`; sin hábitos registrados, listas vacías y `diet_pattern`/`version`
en `null`. Un timeout, un error de red, un `404` o cualquier otra respuesta es
indisponibilidad: el plan nunca se crea sin validar las restricciones (ADR-037). Se llama
fuera de cualquier transacción local. Los valores nunca se registran en logs.
"""

import logging
from typing import Any, NoReturn
from uuid import UUID

import httpx

from sportapp_platform.correlation import CORRELATION_HEADER, current_correlation_id
from training_management.application.errors import NutritionRestrictionsUnavailable
from training_management.domain.nutrition.model import NutritionRestrictions

_logger = logging.getLogger("sportapp.training")


def _text_list(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise NutritionRestrictionsUnavailable()
    return tuple(value)


def _restrictions(document: Any) -> NutritionRestrictions:
    if not isinstance(document, dict):
        raise NutritionRestrictionsUnavailable()
    diet_pattern, version = document.get("diet_pattern"), document.get("version")
    if diet_pattern is not None and not isinstance(diet_pattern, str):
        raise NutritionRestrictionsUnavailable()
    if version is not None and (not isinstance(version, int) or isinstance(version, bool)):
        raise NutritionRestrictionsUnavailable()
    return NutritionRestrictions(
        restrictions=_text_list(document.get("restrictions")),
        allergies=_text_list(document.get("allergies")),
        diet_pattern=diet_pattern,
        version=version,
    )


class UsersNutritionRestrictions:
    def __init__(self, http: httpx.Client) -> None:
        self._http = http

    def get(self, athlete_id: UUID) -> NutritionRestrictions:
        correlation_id = current_correlation_id()
        headers = {CORRELATION_HEADER: correlation_id} if correlation_id else {}
        try:
            response = self._http.get(
                f"/v1/users/{athlete_id}/nutrition-restrictions", headers=headers
            )
        except httpx.TimeoutException:
            self._unavailable("timeout")
        except httpx.HTTPError:
            self._unavailable("transport_error")
        if response.status_code != httpx.codes.OK:
            self._unavailable(f"status_{response.status_code}")
        try:
            document = response.json()
        except ValueError:
            self._unavailable("invalid_json")
        try:
            return _restrictions(document)
        except NutritionRestrictionsUnavailable:
            self._unavailable("invalid_document")

    @staticmethod
    def _unavailable(reason: str) -> NoReturn:
        # Solo el motivo técnico: ni el cuerpo ni los valores de la respuesta.
        _logger.warning(
            "training.nutrition_restrictions_unavailable", extra={"fields": {"reason": reason}}
        )
        raise NutritionRestrictionsUnavailable()
