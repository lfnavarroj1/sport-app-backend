"""HU010 CA4: consultar un plan desde la base de lectura.

Solo lo ven el profesional que lo creó (con su permiso de autor) y el deportista
asignado (con la capacidad vigente). Para cualquier otro usuario el plan "no
existe": la respuesta no revela planes ajenos. Mientras la proyección no termina
también responde que no existe; la confirmación inmediata la da el comando.
"""

from uuid import UUID

from training_management.application.access import (
    require_plan_author,
    require_plan_capability,
)
from training_management.application.errors import TrainingPlanNotFound
from training_management.application.ports import (
    Caller,
    CapabilityChecker,
    PermissionPolicy,
    PlanReader,
    PlanView,
)


class GetTrainingPlan:
    def __init__(
        self,
        *,
        reader: PlanReader,
        permissions: PermissionPolicy,
        capabilities: CapabilityChecker,
    ) -> None:
        self._reader = reader
        self._permissions = permissions
        self._capabilities = capabilities

    def __call__(self, caller: Caller, plan_id: UUID) -> PlanView:
        plan = self._reader.get(plan_id)
        if plan is None:
            raise TrainingPlanNotFound()
        if caller.user_id == plan.professional_id:
            require_plan_author(caller, self._permissions)
            return plan
        if caller.user_id == plan.athlete_id:
            require_plan_capability(caller, self._capabilities)
            return plan
        raise TrainingPlanNotFound()
