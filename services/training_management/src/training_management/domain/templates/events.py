"""`plantilla_entrenamiento.publicada.v1` y `.retirada.v1` en `sportapp.entrenamientos.v1`
(ADR-041, HU028).

- Agregado `training_plan_template`; `aggregate_id` y clave de orden: `template_id`; versión 1
  al publicar y la siguiente al retirar.
- `causation_id`: la `Idempotency-Key` al publicar; al retirar, el `template_id` (el retiro no
  lleva clave: lo protege `expected_version`).
- El payload es el estado completo de la plantilla (el mismo documento de la respuesta), para
  que `entrenamientos-read-projector` construya `training_plan_template_view` aplicando solo
  versiones mayores. Sin datos de deportistas; instantes en UTC con sufijo `Z`.
"""

from datetime import UTC, datetime
from typing import Any

from training_management.domain.templates.model import TemplateStatus, TrainingPlanTemplate

PUBLISHED_EVENT_TYPE = "plantilla_entrenamiento.publicada.v1"
RETIRED_EVENT_TYPE = "plantilla_entrenamiento.retirada.v1"
SCHEMA_VERSION = 1
AGGREGATE_TYPE = "training_plan_template"
EVENT_TYPE_BY_STATUS = {
    TemplateStatus.PUBLISHED: PUBLISHED_EVENT_TYPE,
    TemplateStatus.RETIRED: RETIRED_EVENT_TYPE,
}


def utc_instant(value: datetime) -> str:
    """ISO 8601 en UTC con sufijo `Z`; la conversión a la zona del usuario es del cliente."""
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def template_document(template: TrainingPlanTemplate) -> dict[str, Any]:
    """Estado completo: respuesta de la API, payload del evento y vista de lectura."""
    return {
        "template_id": str(template.template_id),
        "professional_id": str(template.professional_id),
        "title": template.title,
        "objectives": template.objectives,
        "sport": template.sport.value,
        "goal": template.goal.value,
        "level": template.level.value,
        "duration_weeks": template.duration_weeks,
        "weekly_sessions": [
            {
                "day_of_week": session.day_of_week.value,
                "description": session.description,
                "duration_minutes": session.duration_minutes,
            }
            for session in template.weekly_sessions
        ],
        "status": template.status.value,
        "version": template.version,
        "created_at": utc_instant(template.created_at),
        "updated_at": utc_instant(template.updated_at),
        "published_at": utc_instant(template.published_at),
        "retired_at": (
            utc_instant(template.retired_at) if template.retired_at is not None else None
        ),
    }
