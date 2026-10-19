"""Agregado `TrainingSession` de HU032 (ADR-029).

- Transiciones: inexistente → `ACTIVE` (versión 1) → `FINISHED` (versión 2); sin pausa.
- El móvil genera `session_id` (UUID v4) e informa los instantes de la acción en UTC
  (`Z`, precisión de segundos). Se rechazan instantes posteriores en más de 5 minutos
  al reloj del backend o anteriores en más de 30 días.
- `ended_at` es igual o posterior a `started_at`; `duration_seconds` la calcula el
  backend como diferencia entera.
"""

import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from uuid import UUID

from training_management.domain.plans.model import Sport
from training_management.domain.sessions.validation import (
    InvalidTrainingSession,
    Issue,
    Violation,
)

FUTURE_TOLERANCE = timedelta(minutes=5)
MAX_AGE = timedelta(days=30)
STARTED_VERSION = 1
UUID_VERSION_4 = 4
FINISHED_VERSION = 2
_UTC_SECONDS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


class TrainingSessionStatus(StrEnum):
    ACTIVE = "ACTIVE"
    FINISHED = "FINISHED"


@dataclass(frozen=True)
class StartInput:
    """Inicio tal como llega; `None` si el campo faltaba o tenía otro tipo JSON."""

    session_id: str | None
    plan_id: str | None
    sport: str | None
    started_at: str | None
    transport_violations: tuple[Violation, ...] = ()


@dataclass(frozen=True)
class StartData:
    session_id: UUID
    plan_id: UUID | None
    sport: Sport
    started_at: datetime


@dataclass(frozen=True)
class TrainingSession:
    session_id: UUID
    athlete_id: UUID
    plan_id: UUID | None
    sport: Sport
    started_at: datetime
    ended_at: datetime | None
    duration_seconds: int | None
    status: TrainingSessionStatus
    version: int
    created_at: datetime
    updated_at: datetime

    @classmethod
    def start(cls, *, athlete_id: UUID, data: StartData, now: datetime) -> "TrainingSession":
        received_at = now.astimezone(UTC)
        return cls(
            session_id=data.session_id,
            athlete_id=athlete_id,
            plan_id=data.plan_id,
            sport=data.sport,
            started_at=data.started_at,
            ended_at=None,
            duration_seconds=None,
            status=TrainingSessionStatus.ACTIVE,
            version=STARTED_VERSION,
            created_at=received_at,
            updated_at=received_at,
        )

    def matches_start(self, data: StartData) -> bool:
        """El mismo inicio (sesión, plan, deporte e instante) que ya se registró."""
        return (
            self.session_id == data.session_id
            and self.plan_id == data.plan_id
            and self.sport is data.sport
            and self.started_at == data.started_at
        )

    def finish(self, *, ended_at: datetime, now: datetime) -> "TrainingSession":
        if self.status is not TrainingSessionStatus.ACTIVE:
            raise ValueError("Solo se finaliza una sesión activa.")
        if ended_at < self.started_at:
            raise InvalidTrainingSession([Violation("ended_at", Issue.ENDS_BEFORE_STARTS)])
        return replace(
            self,
            ended_at=ended_at,
            duration_seconds=int((ended_at - self.started_at).total_seconds()),
            status=TrainingSessionStatus.FINISHED,
            version=FINISHED_VERSION,
            updated_at=now.astimezone(UTC),
        )


def parse_instant(
    value: str | None, field: str, now: datetime, violations: list[Violation]
) -> datetime | None:
    """Instante UTC `YYYY-MM-DDTHH:MM:SSZ` dentro de la ventana permitida."""
    if value is None:
        return None
    if not _UTC_SECONDS.match(value):
        violations.append(Violation(field, Issue.INVALID_DATETIME))
        return None
    try:
        instant = datetime.fromisoformat(value)
    except ValueError:
        violations.append(Violation(field, Issue.INVALID_DATETIME))
        return None
    if instant > now + FUTURE_TOLERANCE:
        violations.append(Violation(field, Issue.TOO_FAR_IN_FUTURE))
        return None
    if instant < now - MAX_AGE:
        violations.append(Violation(field, Issue.TOO_OLD))
        return None
    return instant.astimezone(UTC)


def parse_uuid(
    value: str | None, field: str, violations: list[Violation], *, version4: bool = False
) -> UUID | None:
    if value is None:
        return None
    try:
        parsed = UUID(value)
    except ValueError:
        violations.append(Violation(field, Issue.INVALID_UUID))
        return None
    if version4 and parsed.version != UUID_VERSION_4:
        violations.append(Violation(field, Issue.INVALID_UUID))
        return None
    return parsed


def validate_start(raw: StartInput, now: datetime) -> StartData:
    """Inicio normalizado o `InvalidTrainingSession` con todas las reglas incumplidas."""
    violations = list(raw.transport_violations)
    session_id = parse_uuid(raw.session_id, "session_id", violations, version4=True)
    plan_id = parse_uuid(raw.plan_id, "plan_id", violations)
    sport = None
    if raw.sport is not None:
        if raw.sport in {item.value for item in Sport}:
            sport = Sport(raw.sport)
        else:
            violations.append(Violation("sport", Issue.UNKNOWN_VALUE))
    started_at = parse_instant(raw.started_at, "started_at", now, violations)
    if violations or session_id is None or sport is None or started_at is None:
        raise InvalidTrainingSession(violations)
    return StartData(session_id=session_id, plan_id=plan_id, sport=sport, started_at=started_at)


def validate_finish(
    raw_session_id: str, ended_at: str | None, transport: tuple[Violation, ...], now: datetime
) -> tuple[UUID, datetime]:
    """Identificador de la ruta e instante final validados (sin conocer la sesión)."""
    violations = list(transport)
    session_id = parse_uuid(raw_session_id, "session_id", violations)
    instant = parse_instant(ended_at, "ended_at", now, violations)
    if violations or session_id is None or instant is None:
        raise InvalidTrainingSession(violations)
    return session_id, instant
