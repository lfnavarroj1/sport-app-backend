"""`plan.entrenamiento_creado.v1` (CMP-003; payload aprobado el 2026-10-12).

Clave de orden y agregado: `plan_id`. El payload lleva el contenido estructurado
que necesita `entrenamientos-read-projector` para construir la vista del plan.
No incluye datos de perfil, nombres, correos ni texto libre clínico.
"""

from typing import Any

from training_management.domain.plans.model import TrainingPlan

EVENT_TYPE = "plan.entrenamiento_creado.v1"
SCHEMA_VERSION = 1
AGGREGATE_TYPE = "training_plan"


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
