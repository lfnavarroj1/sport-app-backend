"""Agregado `TrainingPlanTemplate` y reglas de HU028 (ADR-041).

Una plantilla no es un plan asignado (HU010): no tiene deportista ni fechas, y sugerirla no la
asigna, no la inicia ni la copia como plan personal.

- `title` de 3 a 120 caracteres (espacios colapsados); `objectives` texto de 20 a 2.000.
- `sport` del catálogo de HU010; `goal` del catálogo de ADR-034 (el `primary_goal` de HU006);
  `level` `BEGINNER`, `INTERMEDIATE` o `ADVANCED`; `duration_weeks` de 1 a 52.
- `weekly_sessions` de 1 a 14, en el orden del profesional, cada una con `day_of_week` (días de
  ADR-034), `description` de 1 a 500 y `duration_minutes` de 10 a 600.
- Nace `PUBLISHED` en versión 1. La única transición es `PUBLISHED` → `RETIRED`, que sube una
  vez la versión; la fila se conserva.

La forma la valida primero el modelo de transporte; aquí se protegen las mismas reglas como
invariantes del agregado, cada una con su campo y motivo.
"""

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID

from training_management.domain.plans.errors import Violation
from training_management.domain.plans.model import Sport

TITLE_MIN, TITLE_MAX = 3, 120
OBJECTIVES_MIN, OBJECTIVES_MAX = 20, 2000
DURATION_WEEKS_MIN, DURATION_WEEKS_MAX = 1, 52
SESSIONS_MIN, SESSIONS_MAX = 1, 14
DESCRIPTION_MIN, DESCRIPTION_MAX = 1, 500
SESSION_MINUTES_MIN, SESSION_MINUTES_MAX = 10, 600
INITIAL_VERSION = 1

# Motivos de `details[].issue` que agrega el dominio a los de la validación de forma.
OUT_OF_RANGE = "out_of_range"
INVALID_LENGTH = "invalid_length"


class Goal(StrEnum):
    """Catálogo de objetivos de ADR-034 (`primary_goal` de HU006)."""

    HEALTH = "HEALTH"
    PERFORMANCE = "PERFORMANCE"
    WEIGHT_MANAGEMENT = "WEIGHT_MANAGEMENT"
    RECREATION = "RECREATION"
    COMPETITION = "COMPETITION"


class Level(StrEnum):
    BEGINNER = "BEGINNER"
    INTERMEDIATE = "INTERMEDIATE"
    ADVANCED = "ADVANCED"


class DayOfWeek(StrEnum):
    """Días de la disponibilidad de ADR-034."""

    MON = "MON"
    TUE = "TUE"
    WED = "WED"
    THU = "THU"
    FRI = "FRI"
    SAT = "SAT"
    SUN = "SUN"


class TemplateStatus(StrEnum):
    PUBLISHED = "PUBLISHED"
    RETIRED = "RETIRED"


class InvalidTemplate(Exception):
    """CA4: la plantilla incumple una regla; informa todas las reglas incumplidas."""

    code = "validation_error"

    def __init__(self, violations: list[Violation]) -> None:
        super().__init__(", ".join(f"{item.field}: {item.issue}" for item in violations))
        self.violations = tuple(violations)


class TemplateNotFound(Exception):
    """La plantilla no existe o es de otro profesional; no se revela cuál (CA3)."""

    code = "template_not_found"


class TemplateVersionConflict(Exception):
    """`expected_version` no coincide con la versión vigente de la plantilla publicada."""

    code = "version_conflict"

    def __init__(self, current_version: int) -> None:
        super().__init__(str(current_version))
        self.current_version = current_version


@dataclass(frozen=True)
class WeeklySession:
    day_of_week: DayOfWeek
    description: str
    duration_minutes: int


@dataclass(frozen=True)
class TemplateInput:
    """Datos de la plantilla con la forma JSON correcta."""

    title: str
    objectives: str
    sport: Sport
    goal: Goal
    level: Level
    duration_weeks: int
    weekly_sessions: tuple[WeeklySession, ...]


@dataclass(frozen=True)
class TrainingPlanTemplate:
    template_id: UUID
    professional_id: UUID
    title: str
    objectives: str
    sport: Sport
    goal: Goal
    level: Level
    duration_weeks: int
    weekly_sessions: tuple[WeeklySession, ...]
    status: TemplateStatus
    version: int
    created_at: datetime
    updated_at: datetime
    published_at: datetime
    retired_at: datetime | None = None

    @classmethod
    def publish(
        cls, *, template_id: UUID, professional_id: UUID, data: TemplateInput, now: datetime
    ) -> "TrainingPlanTemplate":
        """Crea la plantilla `PUBLISHED` en versión 1, o falla con todas las reglas incumplidas."""
        title = " ".join(data.title.split())
        objectives = data.objectives.strip()
        sessions = tuple(
            WeeklySession(
                day_of_week=session.day_of_week,
                description=session.description.strip(),
                duration_minutes=session.duration_minutes,
            )
            for session in data.weekly_sessions
        )
        violations = _violations(title, objectives, data.duration_weeks, sessions)
        if violations:
            raise InvalidTemplate(violations)
        published_at = now.astimezone(UTC)
        return cls(
            template_id=template_id,
            professional_id=professional_id,
            title=title,
            objectives=objectives,
            sport=data.sport,
            goal=data.goal,
            level=data.level,
            duration_weeks=data.duration_weeks,
            weekly_sessions=sessions,
            status=TemplateStatus.PUBLISHED,
            version=INITIAL_VERSION,
            created_at=published_at,
            updated_at=published_at,
            published_at=published_at,
        )

    def retire(self, now: datetime) -> "TrainingPlanTemplate":
        """`PUBLISHED` → `RETIRED`: una sola subida de versión; la plantilla se conserva."""
        if self.status is not TemplateStatus.PUBLISHED:
            raise ValueError("Solo una plantilla publicada se puede retirar.")
        retired_at = now.astimezone(UTC)
        return replace(
            self,
            status=TemplateStatus.RETIRED,
            version=self.version + 1,
            updated_at=retired_at,
            retired_at=retired_at,
        )


def session_violations(index: int, session: WeeklySession) -> list[Violation]:
    prefix = f"weekly_sessions.{index}"
    violations: list[Violation] = []
    if not DESCRIPTION_MIN <= len(session.description) <= DESCRIPTION_MAX:
        violations.append(Violation(f"{prefix}.description", INVALID_LENGTH))
    if not SESSION_MINUTES_MIN <= session.duration_minutes <= SESSION_MINUTES_MAX:
        violations.append(Violation(f"{prefix}.duration_minutes", OUT_OF_RANGE))
    return violations


def _violations(
    title: str, objectives: str, duration_weeks: int, sessions: tuple[WeeklySession, ...]
) -> list[Violation]:
    violations: list[Violation] = []
    if not TITLE_MIN <= len(title) <= TITLE_MAX:
        violations.append(Violation("title", INVALID_LENGTH))
    if not OBJECTIVES_MIN <= len(objectives) <= OBJECTIVES_MAX:
        violations.append(Violation("objectives", INVALID_LENGTH))
    if not DURATION_WEEKS_MIN <= duration_weeks <= DURATION_WEEKS_MAX:
        violations.append(Violation("duration_weeks", OUT_OF_RANGE))
    if not SESSIONS_MIN <= len(sessions) <= SESSIONS_MAX:
        violations.append(Violation("weekly_sessions", OUT_OF_RANGE))
    for index, session in enumerate(sessions):
        violations.extend(session_violations(index, session))
    return violations
