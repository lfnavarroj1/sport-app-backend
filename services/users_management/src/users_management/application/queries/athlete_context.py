"""ADR-041: contexto del deportista para las sugerencias (contrato interno, HU026).

`bff-mobile` lo consulta una vez por solicitud de sugerencias y pasa los valores como filtros
explícitos al dominio, que no conoce Usuarios. Lo reutilizarán HU027 a HU030, HU036 y HU039.

Devuelve solo `sports` y `primary_goal` (HU006) y `city`, `country_code` y `birth_date`
(HU005) proyectados en `athlete_profile_view`; ninguna otra sección del perfil (ni datos
fisiológicos ni hábitos alimenticios). Una cuenta activa sin perfil responde con los campos
vacíos (el BFF lo trata como sugerencia no personalizada); una cuenta inexistente o
deshabilitada no se resuelve.

Cada lectura se audita (`audit.athlete_context_read`) con `user_id`, el consumidor y el
resultado, nunca con valores. El consumidor es fijo, `bff-mobile`, el único de HU026.
"""

import logging
from uuid import UUID

from users_management.application.errors import ReadModelUnavailable
from users_management.application.ports import AthleteContext, AthleteContextReader

CONSUMER = "bff-mobile"

_audit = logging.getLogger("sportapp.audit")


class GetAthleteContext:
    def __init__(self, reader: AthleteContextReader) -> None:
        self._reader = reader

    def __call__(self, user_id: UUID) -> AthleteContext | None:
        try:
            found = self._reader.find(user_id)
        except ReadModelUnavailable:
            self._audit(user_id, "unavailable")
            raise
        if found is None:
            result = "user_not_found"
        elif found.is_empty:
            result = "context_missing"
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
        _audit.info("audit.athlete_context_read", extra={"fields": fields})
