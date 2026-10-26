"""Reglas deterministas de sugerencia de plantillas de plan (ADR-041, HU028 CA1 a CA3).

- Solo plantillas `PUBLISHED`: una retirada nunca se sugiere.
- Personalizada (el deportista registró deportes en HU006): plantillas cuyo `sport` está entre
  sus deportes; primero las que coinciden con su `primary_goal`, luego `published_at` desc y
  `template_id` desc. Cada una lleva `SPORT_MATCH` y, si coincide el objetivo, `GOAL_MATCH`.
- Con deportes y sin objetivo, la lista sigue personalizada por deporte (decisión del
  responsable del 2026-10-24 ante HU028 CA2: «`SPORT_MATCH` obligatorio para personalizar,
  `GOAL_MATCH` prioriza»).
- No personalizada (sin deportes, o sin contexto porque Usuarios no respondió): las últimas
  publicadas, sin motivos.
Nada se persiste: la sugerencia se calcula en cada consulta sobre la vista de lectura.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from training_management.domain.plans.model import Sport
from training_management.domain.templates.model import Goal


class TemplateReason(StrEnum):
    SPORT_MATCH = "SPORT_MATCH"
    GOAL_MATCH = "GOAL_MATCH"


@dataclass(frozen=True)
class TemplateSuggestionContext:
    """Contexto del deportista que `bff-mobile` obtiene de Usuarios y pasa como filtros."""

    sports: tuple[Sport, ...]
    primary_goal: Goal | None = None

    @property
    def personalized(self) -> bool:
        return bool(self.sports)


def reasons_for(context: TemplateSuggestionContext, goal: Goal) -> tuple[TemplateReason, ...]:
    if not context.personalized:
        return ()
    if context.primary_goal is not None and goal is context.primary_goal:
        return (TemplateReason.SPORT_MATCH, TemplateReason.GOAL_MATCH)
    return (TemplateReason.SPORT_MATCH,)


def personalized_order(
    context: TemplateSuggestionContext,
    *,
    goal: Goal,
    published_at: datetime,
    template_id: UUID,
) -> tuple[int, float, int]:
    """Clave de orden ascendente: objetivo coincidente, `published_at` y `template_id` desc."""
    goal_rank = 0 if context.primary_goal is not None and goal is context.primary_goal else 1
    return (goal_rank, -published_at.timestamp(), -template_id.int)
