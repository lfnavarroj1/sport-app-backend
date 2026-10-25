"""Base de lectura de HU042: filas de `health_report_view` (ADR-037).

La consulta del historial (HU012) queda fuera de HU042; esta vista solo se construye.
"""

from datetime import datetime
from typing import Any

from training_management.domain.health.model import HealthReport


def health_report_values(report: HealthReport, projected_at: datetime) -> dict[str, Any]:
    return {
        "report_id": report.report_id,
        "athlete_id": report.athlete_id,
        "session_id": report.session_id,
        "type": report.type.value,
        "body_area": report.body_area.value,
        "severity": report.severity.value,
        "occurred_at": report.occurred_at,
        "expected_recovery_days": report.expected_recovery_days,
        "status": report.status.value,
        "version": report.version,
        "reported_at": report.reported_at,
        "projected_at": projected_at,
    }
