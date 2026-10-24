"""Reconstrucción validada de una plantilla de plan desde su documento de estado completo (HU028).

El mismo documento es la respuesta de la API, el resultado guardado para la idempotencia, el
payload de `plantilla_entrenamiento.*.v1` y la fila de `training_plan_template_view`. Un
documento que no cumple las reglas del agregado lanza `InvalidTemplateDocument` con un motivo
sin datos de la plantilla (el proyector lo usa como motivo de rechazo hacia la DLQ).
"""

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from training_management.domain.plans.model import Sport
from training_management.domain.templates.model import (
    DURATION_WEEKS_MAX,
    DURATION_WEEKS_MIN,
    OBJECTIVES_MAX,
    OBJECTIVES_MIN,
    SESSIONS_MAX,
    SESSIONS_MIN,
    TITLE_MAX,
    TITLE_MIN,
    DayOfWeek,
    Goal,
    Level,
    TemplateStatus,
    TrainingPlanTemplate,
    WeeklySession,
    session_violations,
)


class InvalidTemplateDocument(ValueError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _uuid(value: object, field: str) -> UUID:
    try:
        return UUID(str(value))
    except ValueError:
        raise InvalidTemplateDocument(f"{field}_not_uuid") from None


def _instant(value: object, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        raise InvalidTemplateDocument(f"{field}_not_datetime") from None
    if parsed.tzinfo is None:
        raise InvalidTemplateDocument(f"{field}_without_offset")
    return parsed


def _text(value: object, field: str, minimum: int, maximum: int) -> str:
    if not isinstance(value, str) or not minimum <= len(value) <= maximum:
        raise InvalidTemplateDocument(f"{field}_invalid")
    return value


def _int(value: object, field: str, minimum: int, maximum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
        raise InvalidTemplateDocument(f"{field}_invalid")
    return value


def _enum[E: StrEnum](kind: type[E], value: object, field: str) -> E:
    if value not in {item.value for item in kind}:
        raise InvalidTemplateDocument(f"{field}_invalid")
    return kind(str(value))


def _session(index: int, value: object) -> WeeklySession:
    if not isinstance(value, dict):
        raise InvalidTemplateDocument("weekly_sessions_invalid")
    description, minutes = value.get("description"), value.get("duration_minutes")
    if (
        not isinstance(description, str)
        or not isinstance(minutes, int)
        or isinstance(minutes, bool)
    ):
        raise InvalidTemplateDocument("weekly_sessions_invalid")
    session = WeeklySession(
        day_of_week=_enum(DayOfWeek, value.get("day_of_week"), "day_of_week"),
        description=description,
        duration_minutes=minutes,
    )
    if session_violations(index, session):
        raise InvalidTemplateDocument("weekly_sessions_invalid")
    return session


def template_from_document(document: dict[str, Any]) -> TrainingPlanTemplate:
    sessions = document.get("weekly_sessions")
    if not isinstance(sessions, list) or not SESSIONS_MIN <= len(sessions) <= SESSIONS_MAX:
        raise InvalidTemplateDocument("weekly_sessions_invalid")
    version = document.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise InvalidTemplateDocument("version_invalid")
    status = _enum(TemplateStatus, document.get("status"), "status")
    retired_raw = document.get("retired_at")
    retired_at = _instant(retired_raw, "retired_at") if retired_raw is not None else None
    if (status is TemplateStatus.RETIRED) != (retired_at is not None):
        raise InvalidTemplateDocument("retired_at_mismatch")
    return TrainingPlanTemplate(
        template_id=_uuid(document.get("template_id"), "template_id"),
        professional_id=_uuid(document.get("professional_id"), "professional_id"),
        title=_text(document.get("title"), "title", TITLE_MIN, TITLE_MAX),
        objectives=_text(document.get("objectives"), "objectives", OBJECTIVES_MIN, OBJECTIVES_MAX),
        sport=_enum(Sport, document.get("sport"), "sport"),
        goal=_enum(Goal, document.get("goal"), "goal"),
        level=_enum(Level, document.get("level"), "level"),
        duration_weeks=_int(
            document.get("duration_weeks"), "duration_weeks", DURATION_WEEKS_MIN, DURATION_WEEKS_MAX
        ),
        weekly_sessions=tuple(_session(index, item) for index, item in enumerate(sessions)),
        status=status,
        version=version,
        created_at=_instant(document.get("created_at"), "created_at"),
        updated_at=_instant(document.get("updated_at"), "updated_at"),
        published_at=_instant(document.get("published_at"), "published_at"),
        retired_at=retired_at,
    )
