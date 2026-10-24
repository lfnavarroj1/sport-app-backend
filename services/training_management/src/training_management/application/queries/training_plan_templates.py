"""HU028: consultas de plantillas sobre la base de lectura (`training_plan_template_view`).

- `GET /v1/me/training-plan-templates`: plantillas del profesional autenticado en cualquier
  estado (permiso `training.plan.author`), orden `created_at` desc, `template_id` desc, con
  cursor opaco.
- `GET /v1/me/suggestions/training-plan-templates`: sugerencias para el deportista con la
  capacidad `training.plans.suggest`. Los deportes y el objetivo llegan como filtros explícitos
  (`bff-mobile` los obtiene de Usuarios). Las reglas están en `domain.templates.suggestions`.

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
    require_plan_author,
    require_template_suggest_capability,
)
from training_management.application.ports import (
    Caller,
    CapabilityChecker,
    MyTemplatesQuery,
    PermissionPolicy,
    TemplatePosition,
    TemplateReader,
    TemplateSuggestionQuery,
)
from training_management.domain.plans.model import Sport
from training_management.domain.templates.model import Goal, TrainingPlanTemplate
from training_management.domain.templates.suggestions import (
    TemplateReason,
    TemplateSuggestionContext,
    reasons_for,
)

DEFAULT_LIMIT = 20
MAX_LIMIT = 50


class InvalidTemplateQuery(Exception):
    """Parámetro de consulta inválido; se informa el parámetro afectado."""

    code = "validation_error"

    def __init__(self, field: str, issue: str) -> None:
        super().__init__(f"{field}: {issue}")
        self.field = field
        self.issue = issue


@dataclass(frozen=True)
class MyTemplatesFilters:
    limit: int = DEFAULT_LIMIT
    cursor: str | None = None


@dataclass(frozen=True)
class TemplatePage:
    items: tuple[TrainingPlanTemplate, ...]
    next_cursor: str | None


@dataclass(frozen=True)
class TemplateSuggestionFilters:
    sports: tuple[Sport, ...] = ()
    primary_goal: Goal | None = None
    limit: int = DEFAULT_LIMIT


@dataclass(frozen=True)
class TemplateSuggestion:
    template: TrainingPlanTemplate
    reasons: tuple[TemplateReason, ...]


@dataclass(frozen=True)
class TemplateSuggestions:
    items: tuple[TemplateSuggestion, ...]
    personalized: bool


def encode_cursor(position: TemplatePosition) -> str:
    raw = json.dumps(
        {"c": position.created_at.isoformat(), "t": str(position.template_id)},
        separators=(",", ":"),
    )
    return base64.urlsafe_b64encode(raw.encode("utf-8")).rstrip(b"=").decode("ascii")


def decode_cursor(cursor: str) -> TemplatePosition:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        document = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
        created_at = datetime.fromisoformat(document["c"])
        template_id = UUID(document["t"])
    except (ValueError, KeyError, TypeError, binascii.Error, UnicodeError):
        raise InvalidTemplateQuery("cursor", "invalid_cursor") from None
    if created_at.tzinfo is None:
        raise InvalidTemplateQuery("cursor", "invalid_cursor")
    return TemplatePosition(created_at=created_at, template_id=template_id)


def _require_limit(limit: int) -> None:
    if not 1 <= limit <= MAX_LIMIT:
        raise InvalidTemplateQuery("limit", "out_of_range")


class ListMyTemplates:
    def __init__(self, *, reader: TemplateReader, permissions: PermissionPolicy) -> None:
        self._reader = reader
        self._permissions = permissions

    def __call__(self, caller: Caller, filters: MyTemplatesFilters) -> TemplatePage:
        require_plan_author(caller, self._permissions)
        _require_limit(filters.limit)
        after = decode_cursor(filters.cursor) if filters.cursor else None
        # Se pide una de más para saber si existe una página siguiente.
        templates = self._reader.list_by_professional(
            MyTemplatesQuery(professional_id=caller.user_id, limit=filters.limit + 1, after=after)
        )
        page = templates[: filters.limit]
        next_cursor = (
            encode_cursor(TemplatePosition(page[-1].created_at, page[-1].template_id))
            if len(templates) > filters.limit
            else None
        )
        return TemplatePage(items=tuple(page), next_cursor=next_cursor)


class SuggestTemplates:
    def __init__(self, *, reader: TemplateReader, capabilities: CapabilityChecker) -> None:
        self._reader = reader
        self._capabilities = capabilities

    def __call__(self, caller: Caller, filters: TemplateSuggestionFilters) -> TemplateSuggestions:
        require_athlete(caller)
        if len(set(filters.sports)) != len(filters.sports):
            raise InvalidTemplateQuery("sport", "duplicate_value")
        _require_limit(filters.limit)
        # Afiliación se consulta solo con una solicitud válida y antes de leer.
        require_template_suggest_capability(caller, self._capabilities)
        context = TemplateSuggestionContext(
            sports=filters.sports, primary_goal=filters.primary_goal
        )
        templates = self._reader.suggest(
            TemplateSuggestionQuery(context=context, limit=filters.limit)
        )
        return TemplateSuggestions(
            items=tuple(
                TemplateSuggestion(template=item, reasons=reasons_for(context, item.goal))
                for item in templates
            ),
            personalized=context.personalized,
        )
