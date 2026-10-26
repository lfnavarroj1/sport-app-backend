"""HU030: consultas de rutinas alimenticias sobre la base de lectura
(`nutrition_routine_template_view`).

- `GET /v1/me/nutrition-routine-templates`: rutinas del profesional autenticado en cualquier
  estado (permiso `nutrition.plan.author`), orden `created_at` desc, `template_id` desc, con
  cursor opaco.
- `GET /v1/me/suggestions/nutrition-routines`: sugerencias para el deportista con la capacidad
  `nutrition.routines.suggest`. Patrón, alergias, deportes y objetivo llegan como filtros
  explícitos (`bff-mobile` los obtiene de Usuarios) y la exclusión ocurre aquí, nunca en el
  cliente. Las reglas están en `domain.routines.suggestions`.

Ambas son eventualmente consistentes con los comandos (CQRS).
"""

import base64
import binascii
import json
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from training_management.application.access import (
    require_athlete,
    require_nutrition_plan_author,
    require_routine_suggest_capability,
)
from training_management.application.ports import (
    Caller,
    CapabilityChecker,
    MyRoutinesQuery,
    PermissionPolicy,
    RoutinePosition,
    RoutineReader,
    RoutineSuggestionQuery,
)
from training_management.domain.plans.model import Sport
from training_management.domain.routines.model import (
    Allergy,
    DietPattern,
    NutritionRoutineTemplate,
)
from training_management.domain.routines.suggestions import (
    RoutineReason,
    RoutineSuggestionContext,
    reasons_for,
)
from training_management.domain.templates.model import Goal

DEFAULT_LIMIT = 20
MAX_LIMIT = 50


class InvalidRoutineQuery(Exception):
    """Parámetro de consulta inválido; se informa el parámetro afectado."""

    code = "validation_error"

    def __init__(self, field: str, issue: str) -> None:
        super().__init__(f"{field}: {issue}")
        self.field = field
        self.issue = issue


@dataclass(frozen=True)
class MyRoutinesFilters:
    limit: int = DEFAULT_LIMIT
    cursor: str | None = None


@dataclass(frozen=True)
class RoutinePage:
    items: tuple[NutritionRoutineTemplate, ...]
    next_cursor: str | None


@dataclass(frozen=True)
class RoutineSuggestionFilters:
    dietary_pattern: DietPattern | None = None
    exclude_allergens: tuple[Allergy, ...] = ()
    sports: tuple[Sport, ...] = ()
    primary_goal: Goal | None = None
    limit: int = DEFAULT_LIMIT


@dataclass(frozen=True)
class RoutineSuggestion:
    routine: NutritionRoutineTemplate
    reasons: tuple[RoutineReason, ...]


@dataclass(frozen=True)
class RoutineSuggestions:
    items: tuple[RoutineSuggestion, ...]
    personalized: bool


def encode_cursor(position: RoutinePosition) -> str:
    raw = json.dumps(
        {"c": position.created_at.isoformat(), "t": str(position.template_id)},
        separators=(",", ":"),
    )
    return base64.urlsafe_b64encode(raw.encode("utf-8")).rstrip(b"=").decode("ascii")


def decode_cursor(cursor: str) -> RoutinePosition:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        document = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
        created_at = datetime.fromisoformat(document["c"])
        template_id = UUID(document["t"])
    except (ValueError, KeyError, TypeError, binascii.Error, UnicodeError):
        raise InvalidRoutineQuery("cursor", "invalid_cursor") from None
    if created_at.tzinfo is None:
        raise InvalidRoutineQuery("cursor", "invalid_cursor")
    return RoutinePosition(created_at=created_at, template_id=template_id)


def _require_limit(limit: int) -> None:
    if not 1 <= limit <= MAX_LIMIT:
        raise InvalidRoutineQuery("limit", "out_of_range")


def _require_filters(filters: RoutineSuggestionFilters) -> None:
    if len(set(filters.exclude_allergens)) != len(filters.exclude_allergens):
        raise InvalidRoutineQuery("exclude_allergens", "duplicate_value")
    if filters.exclude_allergens and filters.dietary_pattern is None:
        # Las alergias solo se conocen junto con los hábitos de HU009, que siempre tienen patrón.
        raise InvalidRoutineQuery("dietary_pattern", "required")
    if len(set(filters.sports)) != len(filters.sports):
        raise InvalidRoutineQuery("sport", "duplicate_value")
    _require_limit(filters.limit)


class ListMyRoutines:
    def __init__(self, *, reader: RoutineReader, permissions: PermissionPolicy) -> None:
        self._reader = reader
        self._permissions = permissions

    def __call__(self, caller: Caller, filters: MyRoutinesFilters) -> RoutinePage:
        require_nutrition_plan_author(caller, self._permissions)
        _require_limit(filters.limit)
        after = decode_cursor(filters.cursor) if filters.cursor else None
        # Se pide una de más para saber si existe una página siguiente.
        routines = self._reader.list_by_professional(
            MyRoutinesQuery(professional_id=caller.user_id, limit=filters.limit + 1, after=after)
        )
        page = routines[: filters.limit]
        next_cursor = (
            encode_cursor(RoutinePosition(page[-1].created_at, page[-1].template_id))
            if len(routines) > filters.limit
            else None
        )
        return RoutinePage(items=tuple(page), next_cursor=next_cursor)


class SuggestRoutines:
    def __init__(self, *, reader: RoutineReader, capabilities: CapabilityChecker) -> None:
        self._reader = reader
        self._capabilities = capabilities

    def __call__(self, caller: Caller, filters: RoutineSuggestionFilters) -> RoutineSuggestions:
        require_athlete(caller)
        _require_filters(filters)
        # Afiliación se consulta solo con una solicitud válida y antes de leer.
        require_routine_suggest_capability(caller, self._capabilities)
        context = RoutineSuggestionContext(
            dietary_pattern=filters.dietary_pattern,
            exclude_allergens=filters.exclude_allergens,
            sports=filters.sports,
            primary_goal=filters.primary_goal,
        )
        routines = self._reader.suggest(
            RoutineSuggestionQuery(context=context, limit=filters.limit)
        )
        return RoutineSuggestions(
            items=tuple(
                RoutineSuggestion(
                    routine=item, reasons=reasons_for(context, goal=item.goal, sport=item.sport)
                )
                for item in routines
            ),
            personalized=context.personalized,
        )
