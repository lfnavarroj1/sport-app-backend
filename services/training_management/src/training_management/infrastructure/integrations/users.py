"""Contratos internos de Usuarios que consume Entrenamientos.

HU036: `UsersAthleteBirthDates` lee `birth_date` del contexto de ADR-041 (al final del módulo).

Restricciones alimenticias del deportista desde el contrato interno de Usuarios (ADR-035).

`GET /v1/users/{user_id}/nutrition-restrictions` (sin token, dentro de la red de la
plataforma, como `POST /v1/users/lookup`) responde `200 {restrictions, allergies,
diet_pattern, version}`; sin hábitos registrados, listas vacías y `diet_pattern`/`version`
en `null`. Un timeout, un error de red, un `404` o cualquier otra respuesta es
indisponibilidad: el plan nunca se crea sin validar las restricciones (ADR-037). Se llama
fuera de cualquier transacción local. Los valores nunca se registran en logs.
"""

import logging
from datetime import date
from typing import Any, NoReturn
from uuid import UUID

import httpx

from sportapp_platform.correlation import CORRELATION_HEADER, current_correlation_id
from training_management.application.errors import (
    AthleteContextUnavailable,
    NutritionRestrictionsUnavailable,
)
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


class UsersAthleteBirthDates:
    """HU036: `birth_date` del contrato interno `GET /v1/users/{user_id}/athlete-context`
    (ADR-041; sin token, dentro de la red de la plataforma) para el umbral por defecto.

    `200` con `birth_date` en `null` (sin perfil demográfico) → `None`. `404 user_not_found`
    para un deportista ya autenticado solo significa que el directorio proyectado de Usuarios
    todavía no lo tiene, así que tampoco hay perfil demográfico proyectado → `None` (umbral
    `171`). Un timeout, un error de red, otra respuesta o un documento inesperado es
    indisponibilidad: el lote no se evalúa con un umbral supuesto. Solo se lee `birth_date`;
    nada se registra en logs.
    """

    def __init__(self, http: httpx.Client) -> None:
        self._http = http

    def birth_date(self, athlete_id: UUID) -> date | None:
        correlation_id = current_correlation_id()
        headers = {CORRELATION_HEADER: correlation_id} if correlation_id else {}
        try:
            response = self._http.get(f"/v1/users/{athlete_id}/athlete-context", headers=headers)
        except httpx.TimeoutException:
            self._unavailable("timeout")
        except httpx.HTTPError:
            self._unavailable("transport_error")
        if response.status_code == httpx.codes.NOT_FOUND:
            return None
        if response.status_code != httpx.codes.OK:
            self._unavailable(f"status_{response.status_code}")
        try:
            document = response.json()
        except ValueError:
            self._unavailable("invalid_json")
        if not isinstance(document, dict) or "birth_date" not in document:
            self._unavailable("invalid_document")
        raw = document["birth_date"]
        if raw is None:
            return None
        if not isinstance(raw, str):
            self._unavailable("invalid_document")
        try:
            return date.fromisoformat(raw)
        except ValueError:
            self._unavailable("invalid_document")

    @staticmethod
    def _unavailable(reason: str) -> NoReturn:
        _logger.warning(
            "training.athlete_context_unavailable", extra={"fields": {"reason": reason}}
        )
        raise AthleteContextUnavailable()
