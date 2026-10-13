"""`plan.entrenamiento_creado.v1` (CMP-003; payload aprobado el 2026-10-12).

Clave de orden y agregado: `plan_id`. El payload lleva el contenido estructurado
que necesita `entrenamientos-read-projector` para construir la vista del plan.
No incluye datos de perfil, nombres, correos ni texto libre clínico.
"""

from typing import Any
from uuid import UUID, uuid5

from training_management.domain.plans.model import TrainingPlan

EVENT_TYPE = "plan.entrenamiento_creado.v1"
SCHEMA_VERSION = 1
AGGREGATE_TYPE = "training_plan"
# HU035 (ADR-031): identificador estable de cada sesión planificada en la base de lectura.
PLANNED_SESSION_NAMESPACE = UUID("3f6b2c8e-91d4-4a57-b0e2-5c7d9a1e8f43")


def planned_session_id(plan_id: UUID, position: int) -> UUID:
    """UUIDv5 de `plan_id` y la posición de la sesión en el plan (orden del evento).

    `plan.entrenamiento_creado.v1` no lleva identificador por sesión; la posición es la
    misma que en la escritura (`training_plan_session.position`), así que la vista se
    reconstruye con los mismos identificadores.
    """
    return uuid5(PLANNED_SESSION_NAMESPACE, f"{plan_id}:{position}")


def plan_created_payload(plan: TrainingPlan) -> dict[str, Any]:
    return {
        "plan_id": str(plan.plan_id),
        "athlete_id": str(plan.athlete_id),
        "professional_id": str(plan.professional_id),
        "title": plan.title,
        "objectives": list(plan.objectives),
        "start_date": plan.start_date.isoformat(),
        "end_date": plan.end_date.isoformat(),
        "status": plan.status.value,
        "plan_version": plan.version,
        "sessions": [
            {
                "scheduled_date": session.scheduled_date.isoformat(),
                "sport": session.sport.value,
                "duration_minutes": session.duration_minutes,
                "intensity_rpe": session.intensity_rpe,
            }
            for session in plan.sessions
        ],
        "created_at": plan.created_at.isoformat(),
    }
