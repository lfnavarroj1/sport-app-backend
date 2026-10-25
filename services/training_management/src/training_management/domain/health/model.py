"""Reporte de molestia, incapacidad o lesión de HU042 (ADR-037).

- El móvil genera `report_id` (UUID v4) e informa `occurred_at` en UTC
  `AAAA-MM-DDTHH:MM:SSZ`; se rechaza más de 5 minutos por delante del reloj del backend o
  más de 365 días de antigüedad. `reported_at` lo asigna el backend.
- `type`, `body_area` y `severity` pertenecen a catálogos cerrados; no hay texto libre.
- `session_id` es opcional; que la sesión sea propia lo comprueba el caso de uso.
- `expected_recovery_days` es opcional, entero de 0 a 365.
- `consent.version` es obligatorio e igual a `HEALTH_DATA_CONSENT_VERSION` (ADR-034).
- El reporte nace `OPEN` en la versión 1; su cierre o seguimiento queda fuera de HU042.
- No se diagnostica ni se recomienda: el backend solo registra datos de catálogo.
"""

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any
from uuid import UUID

from training_management.domain.health.validation import InvalidHealthReport, Issue, Violation

FUTURE_TOLERANCE = timedelta(minutes=5)
MAX_AGE = timedelta(days=365)
RECOVERY_DAYS_MIN = 0
RECOVERY_DAYS_MAX = 365
CONSENT_VERSION_MAX = 64
REGISTERED_VERSION = 1
UUID_VERSION_4 = 4
REQUEST_FIELDS = (
    "report_id",
    "type",
    "body_area",
    "severity",
    "occurred_at",
    "session_id",
    "expected_recovery_days",
    "consent",
)
CONSENT_FIELDS = ("version",)
_UTC_SECONDS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


class ReportType(StrEnum):
    DISCOMFORT = "DISCOMFORT"
    INJURY = "INJURY"
    INCAPACITY = "INCAPACITY"


class BodyArea(StrEnum):
    HEAD = "HEAD"
    NECK = "NECK"
    SHOULDER = "SHOULDER"
    ARM = "ARM"
    ELBOW = "ELBOW"
    WRIST_HAND = "WRIST_HAND"
    CHEST = "CHEST"
    BACK = "BACK"
    HIP = "HIP"
    THIGH = "THIGH"
    KNEE = "KNEE"
    LOWER_LEG = "LOWER_LEG"
    ANKLE_FOOT = "ANKLE_FOOT"
    OTHER = "OTHER"


class Severity(StrEnum):
    LOW = "LOW"
    MODERATE = "MODERATE"
    HIGH = "HIGH"


class ReportStatus(StrEnum):
    OPEN = "OPEN"


def utc_instant(value: datetime) -> str:
    """ISO 8601 en UTC con sufijo `Z`; la conversión a la zona del usuario es del cliente."""
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class HealthReportData:
    """Solicitud validada y normalizada; base de la idempotencia por `report_id`."""

    report_id: UUID
    type: ReportType
    body_area: BodyArea
    severity: Severity
    occurred_at: datetime
    session_id: UUID | None
    expected_recovery_days: int | None
    consent_version: str

    @property
    def fingerprint(self) -> str:
        """Huella de los datos del reporte (sin el identificador ni la clave)."""
        canonical = json.dumps(
            [
                self.type.value,
                self.body_area.value,
                self.severity.value,
                utc_instant(self.occurred_at),
                str(self.session_id) if self.session_id is not None else None,
                self.expected_recovery_days,
                self.consent_version,
            ],
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class HealthReport:
    report_id: UUID
    athlete_id: UUID
    type: ReportType
    body_area: BodyArea
    severity: Severity
    occurred_at: datetime
    session_id: UUID | None
    expected_recovery_days: int | None
    consent_version: str
    reported_at: datetime
    status: ReportStatus = ReportStatus.OPEN
    version: int = REGISTERED_VERSION

    @classmethod
    def register(cls, *, athlete_id: UUID, data: HealthReportData, now: datetime) -> "HealthReport":
        return cls(
            report_id=data.report_id,
            athlete_id=athlete_id,
            type=data.type,
            body_area=data.body_area,
            severity=data.severity,
            occurred_at=data.occurred_at,
            session_id=data.session_id,
            expected_recovery_days=data.expected_recovery_days,
            consent_version=data.consent_version,
            reported_at=now.astimezone(UTC).replace(microsecond=0),
        )

    @property
    def data(self) -> HealthReportData:
        return HealthReportData(
            report_id=self.report_id,
            type=self.type,
            body_area=self.body_area,
            severity=self.severity,
            occurred_at=self.occurred_at,
            session_id=self.session_id,
            expected_recovery_days=self.expected_recovery_days,
            consent_version=self.consent_version,
        )

    def matches(self, athlete_id: UUID, data: HealthReportData) -> bool:
        """Mismo deportista y mismos datos normalizados que ya se registraron."""
        return self.athlete_id == athlete_id and self.data.fingerprint == data.fingerprint


def _uuid(value: object, field: str, violations: list[Violation], *, required: bool) -> UUID | None:
    if value is None:
        if required:
            violations.append(Violation(field, Issue.REQUIRED))
        return None
    try:
        parsed = UUID(value) if isinstance(value, str) else None
    except ValueError:
        parsed = None
    if parsed is None or parsed.version != UUID_VERSION_4:
        violations.append(Violation(field, Issue.INVALID_UUID))
        return None
    return parsed


def _member[E: (ReportType, BodyArea, Severity)](
    kind: type[E], value: object, field: str, violations: list[Violation]
) -> E | None:
    if value is None:
        violations.append(Violation(field, Issue.REQUIRED))
        return None
    if not isinstance(value, str) or value not in {item.value for item in kind}:
        violations.append(Violation(field, Issue.UNKNOWN_VALUE))
        return None
    return kind(value)


def _occurred_at(value: object, now: datetime, violations: list[Violation]) -> datetime | None:
    if value is None:
        violations.append(Violation("occurred_at", Issue.REQUIRED))
        return None
    if not isinstance(value, str) or not _UTC_SECONDS.match(value):
        violations.append(Violation("occurred_at", Issue.INVALID_DATETIME))
        return None
    try:
        instant = datetime.fromisoformat(value)
    except ValueError:
        violations.append(Violation("occurred_at", Issue.INVALID_DATETIME))
        return None
    if instant > now + FUTURE_TOLERANCE:
        violations.append(Violation("occurred_at", Issue.TOO_FAR_IN_FUTURE))
        return None
    if instant < now - MAX_AGE:
        violations.append(Violation("occurred_at", Issue.TOO_OLD))
        return None
    return instant.astimezone(UTC)


def _recovery_days(value: object, violations: list[Violation]) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        violations.append(Violation("expected_recovery_days", Issue.UNKNOWN_VALUE))
        return None
    if not RECOVERY_DAYS_MIN <= value <= RECOVERY_DAYS_MAX:
        violations.append(Violation("expected_recovery_days", Issue.OUT_OF_RANGE))
        return None
    return value


def _consent(value: object, current_version: str, violations: list[Violation]) -> str | None:
    """`consent.version` obligatorio e igual a la versión vigente (ADR-034, ADR-037)."""
    if value is None:
        violations.append(Violation("consent.version", Issue.REQUIRED))
        return None
    if not isinstance(value, dict):
        violations.append(Violation("consent", Issue.UNKNOWN_VALUE))
        return None
    violations.extend(
        Violation(f"consent.{name}", Issue.FIELD_NOT_ALLOWED)
        for name in value
        if name not in CONSENT_FIELDS
    )
    version = value.get("version")
    if version is None:
        violations.append(Violation("consent.version", Issue.REQUIRED))
        return None
    if not isinstance(version, str):
        violations.append(Violation("consent.version", Issue.UNKNOWN_VALUE))
        return None
    if version != current_version:
        violations.append(Violation("consent.version", Issue.UNSUPPORTED_CONSENT_VERSION))
        return None
    return version


def validate_report(
    body: object,
    now: datetime,
    consent_version: str,
    transport_violations: tuple[Violation, ...] = (),
) -> HealthReportData:
    """Solicitud normalizada o `InvalidHealthReport` con todas las reglas incumplidas.

    `body` es el JSON recibido; `athlete_id`, `status`, `version` u otro campo no previsto
    es `field_not_allowed` porque la identidad sale del token y el estado del backend.
    """
    violations = list(transport_violations)
    if not isinstance(body, dict):
        violations.append(Violation(None, Issue.REQUIRED if body is None else Issue.UNKNOWN_VALUE))
        raise InvalidHealthReport(violations)
    document: dict[str, Any] = body
    violations.extend(
        Violation(name, Issue.FIELD_NOT_ALLOWED) for name in document if name not in REQUEST_FIELDS
    )
    report_id = _uuid(document.get("report_id"), "report_id", violations, required=True)
    report_type = _member(ReportType, document.get("type"), "type", violations)
    body_area = _member(BodyArea, document.get("body_area"), "body_area", violations)
    severity = _member(Severity, document.get("severity"), "severity", violations)
    occurred_at = _occurred_at(document.get("occurred_at"), now, violations)
    session_id = _uuid(document.get("session_id"), "session_id", violations, required=False)
    recovery_days = _recovery_days(document.get("expected_recovery_days"), violations)
    consent = _consent(document.get("consent"), consent_version, violations)
    if (
        violations
        or report_id is None
        or report_type is None
        or body_area is None
        or severity is None
        or occurred_at is None
        or consent is None
    ):
        raise InvalidHealthReport(violations)
    return HealthReportData(
        report_id=report_id,
        type=report_type,
        body_area=body_area,
        severity=severity,
        occurred_at=occurred_at,
        session_id=session_id,
        expected_recovery_days=recovery_days,
        consent_version=consent,
    )
