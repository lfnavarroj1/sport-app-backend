"""HU013 (ADR-037): historial de alimentación sobre `food_intake_view` (patrón HU011).

- El deportista consulta sus propios consumos (opción base, sin capacidad).
- El profesional necesita el permiso `nutrition.history.view_assigned` y una autorización
  vigente del deportista (HU010), leída de la vista proyectada; si falta cualquiera de los
  dos recibe `403` sin revelar si el deportista existe (CA3). Cada consulta del profesional,
  concedida o rechazada, se audita con `audit.nutrition_history_access` sin contenido de los
  consumos.
- Filtros por periodo (fechas de consumo inclusivas, en UTC) y tipo de comida; orden estable
  `consumed_at` desc, `intake_id` desc, y paginación por cursor (keyset) que no pierde ni
  repite consumos entre páginas (CA1, CA4). Periodo o cursor inválidos → `422` (CA2).
- Sin consumos devuelve una colección vacía explícita (CA5). Sin cálculo nutricional: cada
  consumo trae `food_code`, cantidad y unidad; los textos los resuelve el cliente con
  `GET /v1/food-catalog`.
"""

import base64
import binascii
import json
import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from uuid import UUID

from training_management.application.access import ATHLETE
from training_management.application.errors import (
    AthleteAuthorizationRequired,
    PermissionRequired,
)
from training_management.application.ports import (
    AuthorizationReader,
    Caller,
    FoodIntakePosition,
    FoodIntakeQuery,
    FoodIntakeReader,
    PermissionPolicy,
)
from training_management.domain.nutrition.intake import FoodIntake
from training_management.domain.nutrition.model import MealType

NUTRITION_HISTORY_PERMISSION = "nutrition.history.view_assigned"
DEFAULT_LIMIT = 20
MAX_LIMIT = 100

_audit = logging.getLogger("sportapp.audit")


class InvalidFoodIntakeQuery(Exception):
    """Periodo, límite o cursor inválido; se informa el parámetro afectado."""

    code = "validation_error"

    def __init__(self, field: str, issue: str) -> None:
        super().__init__(f"{field}: {issue}")
        self.field = field
        self.issue = issue


@dataclass(frozen=True)
class FoodIntakeFilters:
    date_from: date | None = None
    date_to: date | None = None
    meal_type: MealType | None = None
    limit: int = DEFAULT_LIMIT
    cursor: str | None = None


@dataclass(frozen=True)
class FoodIntakePage:
    items: tuple[FoodIntake, ...]
    next_cursor: str | None


def encode_cursor(position: FoodIntakePosition) -> str:
    raw = json.dumps(
        {"c": position.consumed_at.isoformat(), "i": str(position.intake_id)},
        separators=(",", ":"),
    )
    return base64.urlsafe_b64encode(raw.encode("utf-8")).rstrip(b"=").decode("ascii")


def decode_cursor(cursor: str) -> FoodIntakePosition:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        document = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
        consumed_at = datetime.fromisoformat(document["c"])
        intake_id = UUID(document["i"])
    except (ValueError, KeyError, TypeError, binascii.Error, UnicodeError):
        raise InvalidFoodIntakeQuery("cursor", "invalid_cursor") from None
    if consumed_at.tzinfo is None:
        raise InvalidFoodIntakeQuery("cursor", "invalid_cursor")
    return FoodIntakePosition(consumed_at=consumed_at, intake_id=intake_id)


def _start_of(day: date) -> datetime:
    return datetime.combine(day, time.min, tzinfo=UTC)


class GetFoodIntakeHistory:
    def __init__(
        self,
        *,
        reader: FoodIntakeReader,
        authorizations: AuthorizationReader,
        permissions: PermissionPolicy,
    ) -> None:
        self._reader = reader
        self._authorizations = authorizations
        self._permissions = permissions

    def own(self, caller: Caller, filters: FoodIntakeFilters) -> FoodIntakePage:
        if caller.actor_type != ATHLETE:
            raise PermissionRequired()
        return self._page(caller.user_id, filters)

    def of_athlete(
        self, caller: Caller, athlete_id: UUID, filters: FoodIntakeFilters
    ) -> FoodIntakePage:
        """Acceso de un profesional a datos de otra persona: se audita el resultado."""
        try:
            if not self._permissions.allows(caller.actor_type, NUTRITION_HISTORY_PERMISSION):
                raise PermissionRequired()
            authorization = self._authorizations.get(athlete_id, caller.user_id)
            if authorization is None or not authorization.is_active:
                raise AthleteAuthorizationRequired()
        except (PermissionRequired, AthleteAuthorizationRequired) as error:
            self._record(caller, athlete_id, f"rejected:{error.code}")
            raise
        page = self._page(athlete_id, filters)
        self._record(caller, athlete_id, "granted")
        return page

    @staticmethod
    def _record(caller: Caller, athlete_id: UUID, result: str) -> None:
        # Solo identificadores y resultado: nunca alimentos, cantidades ni filtros.
        _audit.info(
            "audit.nutrition_history_access",
            extra={
                "fields": {
                    "result": result,
                    "professional_id": str(caller.user_id),
                    "athlete_id": str(athlete_id),
                }
            },
        )

    def _page(self, athlete_id: UUID, filters: FoodIntakeFilters) -> FoodIntakePage:
        if not 1 <= filters.limit <= MAX_LIMIT:
            raise InvalidFoodIntakeQuery("limit", "out_of_range")
        if filters.date_from and filters.date_to and filters.date_to < filters.date_from:
            raise InvalidFoodIntakeQuery("to", "to_before_from")
        after = decode_cursor(filters.cursor) if filters.cursor else None
        # Se pide uno de más para saber si existe una página siguiente.
        intakes = self._reader.list(
            FoodIntakeQuery(
                athlete_id=athlete_id,
                consumed_from=_start_of(filters.date_from) if filters.date_from else None,
                consumed_before=_start_of(filters.date_to + timedelta(days=1))
                if filters.date_to
                else None,
                meal_type=filters.meal_type.value if filters.meal_type else None,
                after=after,
                limit=filters.limit + 1,
            )
        )
        page = intakes[: filters.limit]
        next_cursor = (
            encode_cursor(FoodIntakePosition(page[-1].consumed_at, page[-1].intake_id))
            if len(intakes) > filters.limit
            else None
        )
        return FoodIntakePage(items=tuple(page), next_cursor=next_cursor)
