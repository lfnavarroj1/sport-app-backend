"""Eventos de la sesión de entrenamiento en `sportapp.entrenamientos.v1` (CMP-003, ADR-029).

- `entrenamiento.iniciado.v1` (HU032): versión de agregado 1, construye la sesión activa.
- `entrenamiento.finalizado.v1` (payload aprobado el 2026-10-12 para HU011): versión 2,
  retira la sesión activa y alimenta el historial.

Clave de orden y agregado: `session_id`. Los payloads no llevan métricas de salud,
ubicación ni mediciones; los instantes van en UTC con sufijo `Z`.
"""

from datetime import UTC, datetime
from typing import Any

from training_management.domain.sessions.model import TrainingSession, TrainingSessionStatus

EVENT_TYPE = "entrenamiento.finalizado.v1"
STARTED_EVENT_TYPE = "entrenamiento.iniciado.v1"
SCHEMA_VERSION = 1
AGGREGATE_TYPE = "training_session"

# Mismo catálogo que el agregado; se conserva el nombre que ya usa el proyector de HU011.
SessionStatus = TrainingSessionStatus


def utc_instant(value: datetime) -> str:
    """ISO 8601 en UTC con sufijo `Z`; la conversión a la zona del usuario es del cliente."""
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def session_started_payload(session: TrainingSession) -> dict[str, Any]:
    return {
        "session_id": str(session.session_id),
        "athlete_id": str(session.athlete_id),
        "plan_id": str(session.plan_id) if session.plan_id else None,
        "sport": session.sport.value,
        "started_at": utc_instant(session.started_at),
        "status": SessionStatus.ACTIVE.value,
    }


def session_finished_payload(session: TrainingSession) -> dict[str, Any]:
    if session.ended_at is None or session.duration_seconds is None:
        raise ValueError("La sesión no está finalizada.")
    return {
        "session_id": str(session.session_id),
        "athlete_id": str(session.athlete_id),
        "plan_id": str(session.plan_id) if session.plan_id else None,
        "sport": session.sport.value,
        "started_at": utc_instant(session.started_at),
        "ended_at": utc_instant(session.ended_at),
        "duration_seconds": session.duration_seconds,
        "status": SessionStatus.FINISHED.value,
    }
