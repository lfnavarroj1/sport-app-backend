"""HU014: consultas de planes de alimentación sobre la base de lectura (`nutrition_plan_view`).

- `GET /v1/nutrition-plans/{plan_id}`: lo ven el profesional que lo creó (con
  `nutrition.plan.author` y la autorización vigente del deportista, leída de la vista
  proyectada de HU011) y el deportista dueño. Otro tipo de actor o un profesional sin el
  permiso recibe `permission_required`; el creador con la autorización revocada,
  `athlete_authorization_required`. Para cualquier otro usuario el plan "no existe" (sin
  enumeración), igual que mientras la proyección no termina.
- `GET /v1/me/nutrition-plans`: planes del deportista autenticado (opción base, sin
  capacidad ni consulta a Afiliación), orden `created_at` desc, `plan_id` desc, con cursor
  opaco (keyset) que no pierde ni repite planes entre páginas.
"""

import base64
import binascii
import json
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from training_management.application.access import (
    ATHLETE,
    require_athlete,
    require_nutrition_plan_author,
)
from training_management.application.errors import (
    AthleteAuthorizationRequired,
    NutritionPlanNotFound,
)
from training_management.application.ports import (
    AuthorizationReader,
    Caller,
    NutritionPlanPosition,
    NutritionPlanQuery,
    NutritionPlanReader,
    PermissionPolicy,
)
from training_management.domain.nutrition.model import NutritionPlan

DEFAULT_LIMIT = 20
MAX_LIMIT = 50


class InvalidNutritionPlanQuery(Exception):
    """Límite o cursor inválido; se informa el parámetro afectado."""

    code = "validation_error"

    def __init__(self, field: str, issue: str) -> None:
        super().__init__(f"{field}: {issue}")
        self.field = field
        self.issue = issue


@dataclass(frozen=True)
class NutritionPlanFilters:
    limit: int = DEFAULT_LIMIT
    cursor: str | None = None


@dataclass(frozen=True)
class NutritionPlanPage:
    items: tuple[NutritionPlan, ...]
    next_cursor: str | None


def encode_cursor(position: NutritionPlanPosition) -> str:
    raw = json.dumps(
        {"c": position.created_at.isoformat(), "p": str(position.plan_id)},
        separators=(",", ":"),
    )
    return base64.urlsafe_b64encode(raw.encode("utf-8")).rstrip(b"=").decode("ascii")


def decode_cursor(cursor: str) -> NutritionPlanPosition:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        document = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
        created_at = datetime.fromisoformat(document["c"])
        plan_id = UUID(document["p"])
    except (ValueError, KeyError, TypeError, binascii.Error, UnicodeError):
        raise InvalidNutritionPlanQuery("cursor", "invalid_cursor") from None
    if created_at.tzinfo is None:
        raise InvalidNutritionPlanQuery("cursor", "invalid_cursor")
    return NutritionPlanPosition(created_at=created_at, plan_id=plan_id)


class GetNutritionPlan:
    def __init__(
        self,
        *,
        reader: NutritionPlanReader,
        authorizations: AuthorizationReader,
        permissions: PermissionPolicy,
    ) -> None:
        self._reader = reader
        self._authorizations = authorizations
        self._permissions = permissions

    def __call__(self, caller: Caller, plan_id: UUID) -> NutritionPlan:
        # El tipo de actor se evalúa antes de leer: no revela si el plan existe.
        if caller.actor_type != ATHLETE:
            require_nutrition_plan_author(caller, self._permissions)
        plan = self._reader.get(plan_id)
        if plan is None:
            raise NutritionPlanNotFound()
        if caller.actor_type == ATHLETE:
            if caller.user_id == plan.athlete_id:
                return plan
            raise NutritionPlanNotFound()
        if caller.user_id != plan.professional_id:
            raise NutritionPlanNotFound()
        authorization = self._authorizations.get(plan.athlete_id, caller.user_id)
        if authorization is None or not authorization.is_active:
            raise AthleteAuthorizationRequired()
        return plan


class ListMyNutritionPlans:
    def __init__(self, *, reader: NutritionPlanReader) -> None:
        self._reader = reader

    def __call__(self, caller: Caller, filters: NutritionPlanFilters) -> NutritionPlanPage:
        require_athlete(caller)
        if not 1 <= filters.limit <= MAX_LIMIT:
            raise InvalidNutritionPlanQuery("limit", "out_of_range")
        after = decode_cursor(filters.cursor) if filters.cursor else None
        # Se pide uno de más para saber si existe una página siguiente.
        plans = self._reader.list(
            NutritionPlanQuery(athlete_id=caller.user_id, after=after, limit=filters.limit + 1)
        )
        page = plans[: filters.limit]
        next_cursor = (
            encode_cursor(NutritionPlanPosition(page[-1].created_at, page[-1].plan_id))
            if len(plans) > filters.limit
            else None
        )
        return NutritionPlanPage(items=tuple(page), next_cursor=next_cursor)
