"""Representación de la sesión de HU032 en respuestas y en el resultado idempotente guardado.

Los instantes se expresan en UTC con sufijo `Z`; `ended_at` y `duration_seconds` son
`null` mientras la sesión está activa.
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from training_management.domain.plans.model import Sport
from training_management.domain.sessions.events import utc_instant
from training_management.domain.sessions.model import TrainingSession, TrainingSessionStatus


def session_document(session: TrainingSession) -> dict[str, Any]:
    return {
        "session_id": str(session.session_id),
        "athlete_id": str(session.athlete_id),
        "plan_id": str(session.plan_id) if session.plan_id else None,
        "sport": session.sport.value,
        "started_at": utc_instant(session.started_at),
        "ended_at": utc_instant(session.ended_at) if session.ended_at else None,
        "duration_seconds": session.duration_seconds,
        "status": session.status.value,
        "version": session.version,
        "created_at": utc_instant(session.created_at),
        "updated_at": utc_instant(session.updated_at),
    }


def session_from_document(document: dict[str, Any]) -> TrainingSession:
    ended_at = document.get("ended_at")
    plan_id = document.get("plan_id")
    return TrainingSession(
        session_id=UUID(document["session_id"]),
        athlete_id=UUID(document["athlete_id"]),
        plan_id=UUID(plan_id) if plan_id else None,
        sport=Sport(document["sport"]),
        started_at=datetime.fromisoformat(document["started_at"]),
        ended_at=datetime.fromisoformat(ended_at) if ended_at else None,
        duration_seconds=document.get("duration_seconds"),
        status=TrainingSessionStatus(document["status"]),
        version=int(document["version"]),
        created_at=datetime.fromisoformat(document["created_at"]),
        updated_at=datetime.fromisoformat(document["updated_at"]),
    )
