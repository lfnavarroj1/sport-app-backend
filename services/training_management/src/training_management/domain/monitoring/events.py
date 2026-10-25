"""`medicion.recibida.v1` y `riesgo.detectado.v1` en `sportapp.entrenamientos.v1` (ADR-044).

- Agregado `training_session`; `aggregate_id` y clave de orden: `session_id` (las dos
  secuencias de la sesión viajan en orden con sus eventos de inicio y fin).
- `medicion.recibida.v1`: un resumen por lote con muestras nuevas, nunca cada muestra. Su
  `aggregate_version` es la `version` incremental del resumen (1, 2, …), una secuencia propia
  del monitoreo que no se mezcla con las versiones 1-2 del ciclo de vida de la sesión: el
  proyector aplica solo la siguiente versión del resumen.
- `riesgo.detectado.v1`: una alerta confirmada; `aggregate_version` es su número de orden en
  la sesión (1, 2, …). La alerta llega al móvil en la respuesta del lote, no por la proyección.
- Datos de salud mínimos: el resumen aprobado (último, máximo y promedio) y la alerta sin
  valores; instantes en UTC con sufijo `Z` y el promedio como cadena con un decimal.
"""

from typing import Any

from training_management.domain.monitoring.model import BatchSummary, RiskAlert
from training_management.domain.sessions.events import AGGREGATE_TYPE, utc_instant

MEASUREMENT_EVENT_TYPE = "medicion.recibida.v1"
RISK_EVENT_TYPE = "riesgo.detectado.v1"
SCHEMA_VERSION = 1

__all__ = [
    "AGGREGATE_TYPE",
    "MEASUREMENT_EVENT_TYPE",
    "RISK_EVENT_TYPE",
    "SCHEMA_VERSION",
    "measurement_payload",
    "risk_payload",
]


def measurement_payload(summary: BatchSummary) -> dict[str, Any]:
    return {
        "session_id": str(summary.session_id),
        "device_id": summary.device_id,
        "window_from": utc_instant(summary.window_from),
        "window_to": utc_instant(summary.window_to),
        "samples_accepted": summary.samples_accepted,
        "last_value": summary.last_value,
        "max_value": summary.max_value,
        "avg_value": str(summary.avg_value),
        "version": summary.version,
    }


def risk_payload(alert: RiskAlert) -> dict[str, Any]:
    return {
        "alert_id": str(alert.alert_id),
        "session_id": str(alert.session_id),
        "device_id": alert.device_id,
        "rule": alert.rule,
        "severity": alert.severity.value,
        "detected_at": utc_instant(alert.detected_at),
    }
