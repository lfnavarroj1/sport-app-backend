"""ADR-035: restricciones alimenticias de un usuario (contrato interno, HU009 y HU014).

Lo consume solo `training-management` al crear un plan de alimentación para bloquear los
alimentos incompatibles (ADR-037); `bff-web` no lo expone, así que un profesional nunca lee
los hábitos alimenticios (HU009 CA4): solo recibe el resultado de la validación.

Desde HU009 devuelve el patrón, las restricciones, las alergias y la versión proyectados en
`athlete_profile_view.nutrition_habits`. Sin hábitos registrados responde «sin hábitos»:
listas vacías, `diet_pattern` y `version` en `null` (contrato de `users-management` 0.8.0,
decisión del responsable del 2026-10-22, confirmada el 2026-10-24 frente al `404` que
proponía HU009 CA5). Una cuenta inexistente o deshabilitada no se resuelve.

Cada lectura se audita (`audit.nutrition_restrictions_read`) con `user_id`, el consumidor y
el resultado, nunca con valores; la correlación la añade el formato del log. El consumidor
es fijo, `training-management`: es el único autorizado por ADR-035 y el contrato no lleva
token (decisión del 2026-10-24).
"""

import logging
from uuid import UUID

from users_management.application.errors import ReadModelUnavailable
from users_management.application.ports import NutritionRestrictions, NutritionRestrictionsReader

CONSUMER = "training-management"

_audit = logging.getLogger("sportapp.audit")


class GetNutritionRestrictions:
    def __init__(self, reader: NutritionRestrictionsReader) -> None:
        self._reader = reader

    def __call__(self, user_id: UUID) -> NutritionRestrictions | None:
        try:
            found = self._reader.find(user_id)
        except ReadModelUnavailable:
            self._audit(user_id, "unavailable")
            raise
        if found is None:
            result = "user_not_found"
        elif found.version is None:
            result = "habits_missing"
        else:
            result = "found"
        self._audit(user_id, result)
        return found

    def reject(self, user_id: UUID, reason: str) -> None:
        """Una solicitud que no debía llegar (p. ej. con el token de un cliente)."""
        self._audit(user_id, "rejected", reason=reason)

    @staticmethod
    def _audit(user_id: UUID, result: str, reason: str | None = None) -> None:
        fields = {"user_id": str(user_id), "consumer": CONSUMER, "result": result}
        if reason is not None:
            fields["reason"] = reason
        _audit.info("audit.nutrition_restrictions_read", extra={"fields": fields})
