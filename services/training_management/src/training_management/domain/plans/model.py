"""Agregado `TrainingPlan` y reglas de coherencia de HU010 (aprobadas el 2026-10-12).

- Título de 1 a 120 caracteres; de 1 a 5 objetivos de 1 a 200 caracteres.
- `start_date` no posterior a `end_date`; periodo máximo de 52 semanas.
- De 1 a 100 sesiones, cada una dentro del periodo, con un deporte del catálogo,
  duración de 1 a 600 minutos e intensidad RPE de 1 a 10 (escala CR10).
- Como máximo 2 sesiones el mismo día.
- El plan nace en versión 1 y estado `ASSIGNED`; editarlo o versionarlo después
  queda fuera de HU010.
"""

from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import StrEnum
from uuid import UUID

from training_management.domain.plans.errors import InvalidTrainingPlan, Violation

TITLE_MAX_LENGTH = 120
OBJECTIVES_MAX = 5
OBJECTIVE_MAX_LENGTH = 200
MAX_PERIOD = timedelta(weeks=52)
SESSIONS_MAX = 100
SESSIONS_PER_DAY_MAX = 2
DURATION_MINUTES_MIN = 1
DURATION_MINUTES_MAX = 600
INTENSITY_RPE_MIN = 1
INTENSITY_RPE_MAX = 10

# Motivos estables de `details[].issue` en `training_plan_invalid`: forman parte del contrato
# publicado (OpenAPI 0.7.0, solicitud de coordinación 1 de HU010). Uno nuevo es un cambio de
# contrato y debe agregarse aquí y en `bff-web`.
PLAN_RULE_ISSUES = (
    "invalid_length",
    "invalid_count",
    "end_date_before_start_date",
    "period_too_long",
    "outside_plan_period",
    "out_of_range",
    "too_many_sessions_per_day",
)


class Sport(StrEnum):
    RUNNING = "running"
    CYCLING = "cycling"
    SWIMMING = "swimming"
    STRENGTH = "strength"
    OTHER = "other"


class PlanStatus(StrEnum):
    ASSIGNED = "ASSIGNED"


@dataclass(frozen=True)
class PlannedSession:
    scheduled_date: date
    sport: Sport
    duration_minutes: int
    intensity_rpe: int


@dataclass(frozen=True)
class TrainingPlan:
    plan_id: UUID
    athlete_id: UUID
    professional_id: UUID
    title: str
    objectives: tuple[str, ...]
    start_date: date
    end_date: date
    sessions: tuple[PlannedSession, ...]
    status: PlanStatus
    version: int
    created_at: datetime

    @classmethod
    def create(
        cls,
        *,
        plan_id: UUID,
        athlete_id: UUID,
        professional_id: UUID,
        title: str,
        objectives: list[str],
        start_date: date,
        end_date: date,
        sessions: list[PlannedSession],
        now: datetime,
    ) -> "TrainingPlan":
        """Crea el plan asignado en versión 1, o falla con todas las reglas incumplidas."""
        normalized_title = " ".join(title.split())
        normalized_objectives = tuple(" ".join(item.split()) for item in objectives)
        ordered_sessions = tuple(
            sorted(sessions, key=lambda session: (session.scheduled_date, session.sport.value))
        )
        violations = [
            *_title_violations(normalized_title),
            *_objective_violations(normalized_objectives),
            *_period_violations(start_date, end_date),
            *_session_violations(sessions, start_date, end_date),
        ]
        if violations:
            raise InvalidTrainingPlan(violations)
        return cls(
            plan_id=plan_id,
            athlete_id=athlete_id,
            professional_id=professional_id,
            title=normalized_title,
            objectives=normalized_objectives,
            start_date=start_date,
            end_date=end_date,
            sessions=ordered_sessions,
            status=PlanStatus.ASSIGNED,
            version=1,
            created_at=now,
        )


def _title_violations(title: str) -> list[Violation]:
    if not 1 <= len(title) <= TITLE_MAX_LENGTH:
        return [Violation("title", "invalid_length")]
    return []


def _objective_violations(objectives: tuple[str, ...]) -> list[Violation]:
    if not 1 <= len(objectives) <= OBJECTIVES_MAX:
        return [Violation("objectives", "invalid_count")]
    return [
        Violation(f"objectives.{index}", "invalid_length")
        for index, objective in enumerate(objectives)
        if not 1 <= len(objective) <= OBJECTIVE_MAX_LENGTH
    ]


def _period_violations(start_date: date, end_date: date) -> list[Violation]:
    if end_date < start_date:
        return [Violation("end_date", "end_date_before_start_date")]
    # Periodo inclusivo: 52 semanas son 364 días contados desde `start_date`.
    if end_date - start_date >= MAX_PERIOD:
        return [Violation("end_date", "period_too_long")]
    return []


def _session_violations(
    sessions: list[PlannedSession], start_date: date, end_date: date
) -> list[Violation]:
    if not 1 <= len(sessions) <= SESSIONS_MAX:
        return [Violation("sessions", "invalid_count")]
    violations: list[Violation] = []
    for index, session in enumerate(sessions):
        prefix = f"sessions.{index}"
        if not start_date <= session.scheduled_date <= end_date:
            violations.append(Violation(f"{prefix}.scheduled_date", "outside_plan_period"))
        if not DURATION_MINUTES_MIN <= session.duration_minutes <= DURATION_MINUTES_MAX:
            violations.append(Violation(f"{prefix}.duration_minutes", "out_of_range"))
        if not INTENSITY_RPE_MIN <= session.intensity_rpe <= INTENSITY_RPE_MAX:
            violations.append(Violation(f"{prefix}.intensity_rpe", "out_of_range"))
    per_day = Counter(session.scheduled_date for session in sessions)
    violations.extend(
        Violation(f"sessions.{index}.scheduled_date", "too_many_sessions_per_day")
        for index, session in enumerate(sessions)
        if per_day[session.scheduled_date] > SESSIONS_PER_DAY_MAX
    )
    return violations
