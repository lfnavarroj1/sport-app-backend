"""Evento `reporte_salud.registrado.v1` en `sportapp.entrenamientos.v1` (ADR-037).

- Agregado `health_report`; `aggregate_id` y clave de orden: `report_id`.
- Versión de agregado 1 (el reporte nace `OPEN`; su cierre queda fuera de HU042);
  `causation_id` = `Idempotency-Key`.
- El payload es el estado completo del reporte (el mismo documento de la respuesta sin
  `version`), para que `entrenamientos-read-projector` construya `health_report_view`
  aplicando solo versiones mayores. Solo catálogos, fechas en UTC `Z` e identificadores:
  sin texto libre, diagnóstico ni otros datos personales.
"""

from typing import Any

from training_management.domain.health.model import HealthReport, utc_instant

EVENT_TYPE = "reporte_salud.registrado.v1"
SCHEMA_VERSION = 1
AGGREGATE_TYPE = "health_report"


def registered_payload(report: HealthReport) -> dict[str, Any]:
    return {
        "report_id": str(report.report_id),
        "athlete_id": str(report.athlete_id),
        "type": report.type.value,
        "body_area": report.body_area.value,
        "severity": report.severity.value,
        "occurred_at": utc_instant(report.occurred_at),
        "session_id": str(report.session_id) if report.session_id is not None else None,
        "expected_recovery_days": report.expected_recovery_days,
        "consent": {"version": report.consent_version},
        "status": report.status.value,
        "reported_at": utc_instant(report.reported_at),
    }
