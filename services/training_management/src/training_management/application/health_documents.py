"""Documento de estado completo del reporte de salud de HU042 (ADR-037).

El mismo documento es la respuesta de la API, el resultado guardado para la idempotencia y,
sin `version`, el payload de `reporte_salud.registrado.v1`. Un documento que no cumple las
reglas del agregado lanza `InvalidHealthReportDocument` con un motivo sin datos de salud (el
proyector lo usa como motivo de rechazo hacia la DLQ).
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from training_management.domain.health.events import registered_payload
from training_management.domain.health.model import (
    CONSENT_VERSION_MAX,
    RECOVERY_DAYS_MAX,
    RECOVERY_DAYS_MIN,
    BodyArea,
    HealthReport,
    ReportStatus,
    ReportType,
    Severity,
)


class InvalidHealthReportDocument(ValueError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def health_report_document(report: HealthReport) -> dict[str, Any]:
    return {**registered_payload(report), "version": report.version}


def _uuid(value: object, field: str) -> UUID:
    try:
        return UUID(str(value))
    except ValueError:
        raise InvalidHealthReportDocument(f"{field}_not_uuid") from None


def _instant(value: object, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        raise InvalidHealthReportDocument(f"{field}_not_datetime") from None
    if parsed.tzinfo is None:
        raise InvalidHealthReportDocument(f"{field}_without_offset")
    return parsed


def _enum[E: (ReportType, BodyArea, Severity, ReportStatus)](
    kind: type[E], value: object, field: str
) -> E:
    if value not in {item.value for item in kind}:
        raise InvalidHealthReportDocument(f"{field}_invalid")
    return kind(str(value))


def _recovery_days(value: object) -> int | None:
    if value is None:
        return None
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not RECOVERY_DAYS_MIN <= value <= RECOVERY_DAYS_MAX
    ):
        raise InvalidHealthReportDocument("expected_recovery_days_invalid")
    return value


def _consent_version(value: object) -> str:
    version = value.get("version") if isinstance(value, dict) else None
    if not isinstance(version, str) or not 1 <= len(version) <= CONSENT_VERSION_MAX:
        raise InvalidHealthReportDocument("consent_invalid")
    return version


def health_report_from_document(document: dict[str, Any]) -> HealthReport:
    version = document.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise InvalidHealthReportDocument("version_invalid")
    session_raw = document.get("session_id")
    return HealthReport(
        report_id=_uuid(document.get("report_id"), "report_id"),
        athlete_id=_uuid(document.get("athlete_id"), "athlete_id"),
        type=_enum(ReportType, document.get("type"), "type"),
        body_area=_enum(BodyArea, document.get("body_area"), "body_area"),
        severity=_enum(Severity, document.get("severity"), "severity"),
        occurred_at=_instant(document.get("occurred_at"), "occurred_at"),
        session_id=_uuid(session_raw, "session_id") if session_raw is not None else None,
        expected_recovery_days=_recovery_days(document.get("expected_recovery_days")),
        consent_version=_consent_version(document.get("consent")),
        reported_at=_instant(document.get("reported_at"), "reported_at"),
        status=_enum(ReportStatus, document.get("status"), "status"),
        version=version,
    )
