"""Errores de negocio de los planes, con códigos estables para el contrato."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Violation:
    """Regla incumplida: campo afectado (ruta del cuerpo) y motivo estable."""

    field: str
    issue: str


class PlanError(Exception):
    code = "plan_error"


class InvalidTrainingPlan(PlanError):
    """CA3: el plan no es coherente; informa todas las reglas incumplidas."""

    code = "training_plan_invalid"

    def __init__(self, violations: list[Violation]) -> None:
        super().__init__(", ".join(f"{item.field}: {item.issue}" for item in violations))
        self.violations = tuple(violations)
