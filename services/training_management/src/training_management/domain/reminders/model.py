"""Reglas y preferencias de los recordatorios en sesión de HU038 (ADR-045 §4-§5).

Ruleset `session-reminders-2026-10-13.v1`, configuración versionada en código e inmutable por
versión (cambiar un valor exige una versión nueva):
- `HYDRATION` cada `interval_minutes` desde el inicio de la sesión (15 por defecto, 10-60);
- `NUTRITION` cada `interval_minutes` (45 por defecto, 30-120), solo cuando la sesión supera
  60 min (`applies_after_minutes`).
Cada regla lleva el `message` aprobado y un `disclaimer`; ninguna diagnostica ni prescribe
cantidades. El backend no evalúa ni envía recordatorios: lo hace el dispositivo (ADR-045 §6).

Preferencias del deportista (`session_reminder_preference`): por tipo, `enabled` e
`interval_minutes` dentro del rango de su regla, con `version` para el control optimista
(`expected_version`). Sin configuración guardada valen los valores por defecto del ruleset con
`enabled: true` y `version: 0` (por ratificar); el primer `PUT` usa `expected_version: 0`.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from training_management.domain.sessions.validation import Issue

RULESET_VERSION = "session-reminders-2026-10-13.v1"
# Texto aprobado por el responsable el 2026-10-25 (paquete HU038; ADR-045 §4 no lo fijaba).
DISCLAIMER = "Recordatorio general: no es un diagnóstico médico ni prescribe cantidades."
OUT_OF_RANGE = "out_of_range"
PREFERENCE_FIELDS = ("hydration", "nutrition", "expected_version")
SETTING_FIELDS = ("enabled", "interval_minutes")


class ReminderType(StrEnum):
    HYDRATION = "HYDRATION"
    NUTRITION = "NUTRITION"


@dataclass(frozen=True)
class ReminderRule:
    type: ReminderType
    default_interval_minutes: int
    min: int
    max: int
    applies_after_minutes: int
    message: str
    disclaimer: str


@dataclass(frozen=True)
class ReminderRuleset:
    ruleset_version: str
    rules: tuple[ReminderRule, ...]

    def rule(self, reminder_type: ReminderType) -> ReminderRule:
        return next(rule for rule in self.rules if rule.type is reminder_type)


RULESET = ReminderRuleset(
    ruleset_version=RULESET_VERSION,
    rules=(
        ReminderRule(
            type=ReminderType.HYDRATION,
            default_interval_minutes=15,
            min=10,
            max=60,
            applies_after_minutes=0,
            message="Hidrátate: bebe agua o bebida isotónica",
            disclaimer=DISCLAIMER,
        ),
        ReminderRule(
            type=ReminderType.NUTRITION,
            default_interval_minutes=45,
            min=30,
            max=120,
            applies_after_minutes=60,
            message="Consume un alimento ligero según tu plan",
            disclaimer=DISCLAIMER,
        ),
    ),
)


class InvalidReminderPreferences(Exception):
    """El cuerpo de `PUT /v1/me/session-reminder-preferences` incumple ADR-045 §5."""

    code = "validation_error"

    def __init__(self, violations: list[tuple[str | None, str]]) -> None:
        super().__init__(", ".join(f"{field}: {issue}" for field, issue in violations))
        self.violations = tuple(violations)


class ReminderPreferencesVersionConflict(Exception):
    """`expected_version` no coincide con la versión vigente de las preferencias."""

    code = "version_conflict"

    def __init__(self, current_version: int) -> None:
        super().__init__(f"versión vigente {current_version}")
        self.current_version = current_version


@dataclass(frozen=True)
class ReminderSetting:
    enabled: bool
    interval_minutes: int


@dataclass(frozen=True)
class PreferencesChange:
    """`PUT` ya validado: preferencias completas y la versión que el cliente leyó."""

    hydration: ReminderSetting
    nutrition: ReminderSetting
    expected_version: int


@dataclass(frozen=True)
class ReminderPreferences:
    athlete_id: UUID
    hydration: ReminderSetting
    nutrition: ReminderSetting
    version: int
    created_at: datetime | None
    updated_at: datetime | None

    @classmethod
    def default(cls, athlete_id: UUID) -> "ReminderPreferences":
        """Sin configuración guardada: valores por defecto del ruleset, versión 0."""
        hydration = RULESET.rule(ReminderType.HYDRATION).default_interval_minutes
        nutrition = RULESET.rule(ReminderType.NUTRITION).default_interval_minutes
        return cls(
            athlete_id=athlete_id,
            hydration=ReminderSetting(True, hydration),
            nutrition=ReminderSetting(True, nutrition),
            version=0,
            created_at=None,
            updated_at=None,
        )

    @property
    def stored(self) -> bool:
        return self.version > 0

    def apply(self, change: PreferencesChange, now: datetime) -> "ReminderPreferences":
        """Versión distinta: conflicto. Mismos valores ya guardados: sin cambios (PUT
        idempotente). Si no: versión siguiente (1 al guardar por primera vez)."""
        if change.expected_version != self.version:
            raise ReminderPreferencesVersionConflict(self.version)
        same = (change.hydration, change.nutrition) == (self.hydration, self.nutrition)
        if same and self.stored:
            return self
        instant = now.astimezone(UTC)
        return ReminderPreferences(
            athlete_id=self.athlete_id,
            hydration=change.hydration,
            nutrition=change.nutrition,
            version=self.version + 1,
            created_at=self.created_at or instant,
            updated_at=instant,
        )


def _is_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _setting(
    name: str, value: object, rule: ReminderRule, violations: list[tuple[str | None, str]]
) -> ReminderSetting | None:
    if value is None:
        violations.append((name, Issue.REQUIRED.value))
        return None
    if not isinstance(value, dict):
        violations.append((name, Issue.UNKNOWN_VALUE.value))
        return None
    before = len(violations)
    violations.extend(
        (f"{name}.{field}", Issue.FIELD_NOT_ALLOWED.value)
        for field in value
        if field not in SETTING_FIELDS
    )
    enabled: Any = value.get("enabled")
    if enabled is None:
        violations.append((f"{name}.enabled", Issue.REQUIRED.value))
    elif not isinstance(enabled, bool):
        violations.append((f"{name}.enabled", Issue.UNKNOWN_VALUE.value))
    interval: Any = value.get("interval_minutes")
    if interval is None:
        violations.append((f"{name}.interval_minutes", Issue.REQUIRED.value))
    elif not _is_integer(interval):
        violations.append((f"{name}.interval_minutes", Issue.UNKNOWN_VALUE.value))
    elif not rule.min <= interval <= rule.max:
        violations.append((f"{name}.interval_minutes", OUT_OF_RANGE))
    if len(violations) > before:
        return None
    return ReminderSetting(enabled, interval)


def validate_preferences(body: object) -> PreferencesChange:
    """El cambio pedido o `InvalidReminderPreferences` con todas las reglas incumplidas."""
    if not isinstance(body, dict):
        issue = Issue.REQUIRED if body is None else Issue.UNKNOWN_VALUE
        raise InvalidReminderPreferences([(None, issue.value)])
    violations: list[tuple[str | None, str]] = [
        (name, Issue.FIELD_NOT_ALLOWED.value) for name in body if name not in PREFERENCE_FIELDS
    ]
    hydration = _setting(
        "hydration", body.get("hydration"), RULESET.rule(ReminderType.HYDRATION), violations
    )
    nutrition = _setting(
        "nutrition", body.get("nutrition"), RULESET.rule(ReminderType.NUTRITION), violations
    )
    expected: Any = body.get("expected_version")
    if expected is None:
        violations.append(("expected_version", Issue.REQUIRED.value))
    elif not _is_integer(expected):
        violations.append(("expected_version", Issue.UNKNOWN_VALUE.value))
    elif expected < 0:
        violations.append(("expected_version", OUT_OF_RANGE))
    if violations or hydration is None or nutrition is None:
        raise InvalidReminderPreferences(violations)
    return PreferencesChange(hydration, nutrition, expected)
