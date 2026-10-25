"""Reglas deterministas de sugerencia de rutinas alimenticias (ADR-041, HU030 CA1 a CA3).

- Solo rutinas `PUBLISHED`: una retirada nunca se sugiere.
- Personalizada (llegó el `dietary_pattern` del deportista, es decir, registró HU009): se
  **excluye** toda rutina cuyo `contains_allergens` intersecte sus alergias o cuyo patrón sea
  incompatible con el suyo (CA2); ninguna se marca «compatible» sin verificarlo. Cada rutina
  lleva `RESTRICTION_COMPATIBLE`, más `GOAL_MATCH` si su objetivo es el `primary_goal` y
  `SPORT_MATCH` si su deporte está entre los del deportista. Orden: objetivo coincidente,
  deporte coincidente, `published_at` desc y `template_id` desc.
- No personalizada (sin hábitos alimenticios o Usuarios sin respuesta): las últimas publicadas,
  sin motivos; `bff-mobile` agrega el aviso de restricciones no verificadas (CA3).

Compatibilidad de patrones (supuesto del 2026-10-25, por ratificar), derivada del catálogo de
alimentos de HU014 (`OMNIVORE` y `FLEXITARIAN` nunca bloquean; la carne bloquea `VEGAN`,
`VEGETARIAN` y `PESCATARIAN`; el pescado `VEGAN` y `VEGETARIAN`; lácteos y huevo `VEGAN`): una
rutina es apta para un patrón si el suyo es igual o más estricto.
Las restricciones (`GLUTEN_FREE`, `KETO`…) no son filtros del contrato de ADR-041.
Nada se persiste: la sugerencia se calcula en cada consulta sobre la vista de lectura.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from training_management.domain.plans.model import Sport
from training_management.domain.routines.model import Allergy, DietPattern
from training_management.domain.templates.model import Goal

_ANY = frozenset(DietPattern)
ACCEPTED_PATTERNS: dict[DietPattern, frozenset[DietPattern]] = {
    DietPattern.OMNIVORE: _ANY,
    DietPattern.FLEXITARIAN: _ANY,
    DietPattern.PESCATARIAN: frozenset(
        {DietPattern.PESCATARIAN, DietPattern.VEGETARIAN, DietPattern.VEGAN}
    ),
    DietPattern.VEGETARIAN: frozenset({DietPattern.VEGETARIAN, DietPattern.VEGAN}),
    DietPattern.VEGAN: frozenset({DietPattern.VEGAN}),
}


class RoutineReason(StrEnum):
    RESTRICTION_COMPATIBLE = "RESTRICTION_COMPATIBLE"
    GOAL_MATCH = "GOAL_MATCH"
    SPORT_MATCH = "SPORT_MATCH"


@dataclass(frozen=True)
class RoutineSuggestionContext:
    """Contexto que `bff-mobile` obtiene de Usuarios y pasa como filtros explícitos. Las
    alergias solo transitan en la solicitud: nunca se persisten ni se registran."""

    dietary_pattern: DietPattern | None = None
    exclude_allergens: tuple[Allergy, ...] = ()
    sports: tuple[Sport, ...] = ()
    primary_goal: Goal | None = None

    @property
    def personalized(self) -> bool:
        return self.dietary_pattern is not None

    @property
    def accepted_patterns(self) -> frozenset[DietPattern]:
        if self.dietary_pattern is None:
            return _ANY
        return ACCEPTED_PATTERNS[self.dietary_pattern]


def is_compatible(
    context: RoutineSuggestionContext,
    *,
    dietary_pattern: DietPattern,
    contains_allergens: tuple[Allergy, ...],
) -> bool:
    """CA2: sin alérgenos del deportista y con un patrón apto para el suyo."""
    if not context.personalized:
        return True
    if set(contains_allergens).intersection(context.exclude_allergens):
        return False
    return dietary_pattern in context.accepted_patterns


def _goal_match(context: RoutineSuggestionContext, goal: Goal) -> bool:
    return context.primary_goal is not None and goal is context.primary_goal


def _sport_match(context: RoutineSuggestionContext, sport: Sport | None) -> bool:
    return sport is not None and sport in context.sports


def reasons_for(
    context: RoutineSuggestionContext, *, goal: Goal, sport: Sport | None
) -> tuple[RoutineReason, ...]:
    if not context.personalized:
        return ()
    reasons = [RoutineReason.RESTRICTION_COMPATIBLE]
    if _goal_match(context, goal):
        reasons.append(RoutineReason.GOAL_MATCH)
    if _sport_match(context, sport):
        reasons.append(RoutineReason.SPORT_MATCH)
    return tuple(reasons)


def suggestion_order(
    context: RoutineSuggestionContext,
    *,
    goal: Goal,
    sport: Sport | None,
    published_at: datetime,
    template_id: UUID,
) -> tuple[int, int, float, int]:
    """Clave de orden ascendente: objetivo, deporte, `published_at` y `template_id` desc. Sin
    personalizar solo cuentan los dos últimos."""
    personalized = context.personalized
    goal_rank = 0 if personalized and _goal_match(context, goal) else 1
    sport_rank = 0 if personalized and _sport_match(context, sport) else 1
    return (goal_rank, sport_rank, -published_at.timestamp(), -template_id.int)
