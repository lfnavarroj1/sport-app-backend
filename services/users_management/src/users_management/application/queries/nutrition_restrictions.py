"""ADR-035: restricciones alimenticias de un usuario (contrato interno, HU014 y ADR-037).

Lo consume solo `training-management` al crear un plan de alimentación para bloquear los
alimentos incompatibles; `bff-web` no lo expone, así que un profesional nunca lee los hábitos
alimenticios (ADR-035, CA4): solo recibe el resultado de la validación.

Mientras HU009 no registre hábitos alimenticios (`nutrition_habits`), toda cuenta activa
responde «sin hábitos»: listas vacías, `diet_pattern` y `version` en `null` (decisión del
responsable del 2026-10-22, como las secciones vacías de `GET /v1/me/profile`). Una cuenta
inexistente o deshabilitada no se resuelve. Cada lectura se audita sin valores.
"""

import logging
from uuid import UUID

from users_management.application.ports import ActiveUserCheck, NutritionRestrictions

_audit = logging.getLogger("sportapp.audit")

NO_HABITS = NutritionRestrictions(restrictions=(), allergies=(), diet_pattern=None, version=None)


class GetNutritionRestrictions:
    def __init__(self, users: ActiveUserCheck) -> None:
        self._users = users

    def __call__(self, user_id: UUID) -> NutritionRestrictions | None:
        found = self._users.is_active(user_id)
        _audit.info(
            "audit.nutrition_restrictions_read",
            extra={
                "fields": {
                    "user_id": str(user_id),
                    "result": "habits_missing" if found else "user_not_found",
                }
            },
        )
        return NO_HABITS if found else None
