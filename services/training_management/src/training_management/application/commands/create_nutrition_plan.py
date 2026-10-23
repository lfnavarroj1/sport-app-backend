"""HU014: crear y asignar un plan de alimentación (ADR-037; contrato del 2026-10-22).

Secuencia:
1. Verificar que quien llama tenga el permiso `nutrition.plan.author` (CA3).
2. Validar las reglas del plan contra el catálogo de alimentos vigente, leído fuera de
   cualquier transacción, sin llamar a Usuarios.
3. Si la clave de idempotencia ya tiene un resultado vigente, devolverlo (sin volver a
   consultar Usuarios: el reintento repite también los `warnings`).
4. Comprobar (lectura) que el deportista autorizó a este profesional: un profesional no
   autorizado nunca provoca la lectura de restricciones.
5. Consultar las restricciones del deportista en Usuarios fuera de la transacción; si no
   responde, `503 upstream_unavailable` sin crear el plan.
6. Bloquear el plan si algún alimento choca con una alergia o una restricción (CA2); sin
   hábitos registrados, se crea con `warnings: ["nutrition_habits_missing"]`.
7. En una sola transacción: autorización (`FOR SHARE`), plan, comidas, ítems, outbox y clave.
8. Si una solicitud concurrente con la misma clave confirmó primero, devolver su resultado.
Las restricciones no se persisten ni se registran. La respuesta se emite tras el COMMIT y no
espera la proyección de lectura.
"""

import hashlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

from training_management.application.access import require_nutrition_plan_author
from training_management.application.errors import (
    AthleteAuthorizationRequired,
    IdempotencyKeyConflict,
    IdempotencyKeyReused,
    NutritionRestrictionsUnavailable,
    PermissionRequired,
)
from training_management.application.nutrition_documents import (
    creation_document,
    plan_from_document,
    warnings_from_document,
)
from training_management.application.ports import (
    Caller,
    Clock,
    FoodCatalogReader,
    IdempotencyRecord,
    NutritionRestrictionsReader,
    NutritionUnitOfWork,
    OutboxMessage,
    PermissionPolicy,
)
from training_management.domain.nutrition.events import (
    AGGREGATE_TYPE,
    EVENT_TYPE,
    SCHEMA_VERSION,
    nutrition_plan_created_payload,
)
from training_management.domain.nutrition.model import (
    InvalidNutritionPlan,
    Meal,
    MealType,
    NutritionPlan,
    PlanItem,
)

OPERATION = "create_nutrition_plan"
IDEMPOTENCY_TTL = timedelta(hours=24)
NUTRITION_HABITS_MISSING = "nutrition_habits_missing"

_audit = logging.getLogger("sportapp.audit")


@dataclass(frozen=True)
class ItemInput:
    food_code: str
    quantity: Decimal
    unit: str


@dataclass(frozen=True)
class MealInput:
    meal_type: MealType
    items: tuple[ItemInput, ...]


@dataclass(frozen=True)
class CreateNutritionPlanCommand:
    athlete_id: UUID
    title: str
    objectives: tuple[str, ...]
    start_date: date
    end_date: date
    meals: tuple[MealInput, ...]
    idempotency_key: UUID
    correlation_id: UUID | None = None


@dataclass(frozen=True)
class CreatedNutritionPlan:
    plan: NutritionPlan
    warnings: tuple[str, ...] = ()
    replayed: bool = False


def request_fingerprint(professional_id: UUID, plan: NutritionPlan) -> str:
    """Identifica la solicitud para detectar reutilización de la clave.

    Incluye al profesional: la clave de otro profesional nunca devuelve un plan ajeno. Usa
    los datos normalizados (`150` y `150.0` son la misma solicitud).
    """
    canonical = json.dumps(
        [
            OPERATION,
            str(professional_id),
            str(plan.athlete_id),
            plan.title,
            list(plan.objectives),
            plan.start_date.isoformat(),
            plan.end_date.isoformat(),
            [
                [
                    meal.meal_type.value,
                    [[item.food_code, str(item.quantity), item.unit] for item in meal.items],
                ]
                for meal in plan.meals
            ],
        ],
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class CreateNutritionPlan:
    def __init__(
        self,
        *,
        unit_of_work: Callable[[], NutritionUnitOfWork],
        permissions: PermissionPolicy,
        catalog: FoodCatalogReader,
        restrictions: NutritionRestrictionsReader,
        clock: Clock,
        new_id: Callable[[], UUID] = uuid4,
    ) -> None:
        self._unit_of_work = unit_of_work
        self._permissions = permissions
        self._catalog = catalog
        self._restrictions = restrictions
        self._clock = clock
        self._new_id = new_id

    def __call__(self, caller: Caller, command: CreateNutritionPlanCommand) -> CreatedNutritionPlan:
        try:
            result = self._execute(caller, command)
        except (
            PermissionRequired,
            InvalidNutritionPlan,
            AthleteAuthorizationRequired,
            IdempotencyKeyReused,
            NutritionRestrictionsUnavailable,
        ) as error:
            # Solo identificadores y el motivo: nunca alimentos, alergias ni restricciones.
            _audit.info(
                "audit.nutrition_plan_created",
                extra={
                    "fields": {
                        "result": "rejected",
                        "reason": error.code,
                        "blocked": isinstance(error, InvalidNutritionPlan) and error.blocked,
                        "professional_id": str(caller.user_id),
                        "athlete_id": str(command.athlete_id),
                    }
                },
            )
            raise
        _audit.info(
            "audit.nutrition_plan_created",
            extra={
                "fields": {
                    "result": "replayed" if result.replayed else "created",
                    "blocked": False,
                    "plan_id": str(result.plan.plan_id),
                    "athlete_id": str(result.plan.athlete_id),
                    "professional_id": str(caller.user_id),
                }
            },
        )
        return result

    def _execute(self, caller: Caller, command: CreateNutritionPlanCommand) -> CreatedNutritionPlan:
        require_nutrition_plan_author(caller, self._permissions)
        catalog = self._catalog.load()
        plan = NutritionPlan.create(
            plan_id=self._new_id(),
            athlete_id=command.athlete_id,
            professional_id=caller.user_id,
            title=command.title,
            objectives=list(command.objectives),
            start_date=command.start_date,
            end_date=command.end_date,
            meals=[
                Meal(
                    meal_type=meal.meal_type,
                    items=tuple(
                        PlanItem(food_code=item.food_code, quantity=item.quantity, unit=item.unit)
                        for item in meal.items
                    ),
                )
                for meal in command.meals
            ],
            catalog=catalog,
            now=self._clock.now(),
        )
        fingerprint = request_fingerprint(caller.user_id, plan)

        previous = self._previous_result(command.idempotency_key, fingerprint)
        if previous is not None:
            return previous
        with self._unit_of_work() as unit_of_work:
            authorized = unit_of_work.has_active_authorization(
                plan.athlete_id, plan.professional_id
            )
        if not authorized:
            raise AthleteAuthorizationRequired()
        restrictions = self._restrictions.get(plan.athlete_id)
        conflicts = plan.conflicts(catalog, restrictions)
        if conflicts:
            raise InvalidNutritionPlan(conflicts)
        warnings = (NUTRITION_HABITS_MISSING,) if restrictions.habits_missing else ()
        try:
            return self._create(command, plan, warnings, fingerprint)
        except IdempotencyKeyConflict:
            # Una solicitud concurrente con la misma clave confirmó primero: su resultado.
            previous = self._previous_result(command.idempotency_key, fingerprint)
            if previous is None:
                raise
            return previous

    def _previous_result(self, key: UUID, fingerprint: str) -> CreatedNutritionPlan | None:
        with self._unit_of_work() as unit_of_work:
            record = unit_of_work.find_idempotency(key)
        if record is None or record.expires_at <= self._clock.now():
            return None
        if record.operation != OPERATION or record.request_fingerprint != fingerprint:
            raise IdempotencyKeyReused()
        return CreatedNutritionPlan(
            plan=plan_from_document(record.response),
            warnings=warnings_from_document(record.response),
            replayed=True,
        )

    def _create(
        self,
        command: CreateNutritionPlanCommand,
        plan: NutritionPlan,
        warnings: tuple[str, ...],
        fingerprint: str,
    ) -> CreatedNutritionPlan:
        now = plan.created_at
        with self._unit_of_work() as unit_of_work:
            existing = unit_of_work.find_idempotency(command.idempotency_key)
            if existing is not None:
                if existing.expires_at > now:
                    raise IdempotencyKeyConflict()
                unit_of_work.delete_idempotency(command.idempotency_key)
            if not unit_of_work.has_active_authorization(plan.athlete_id, plan.professional_id):
                raise AthleteAuthorizationRequired()
            unit_of_work.add_nutrition_plan(plan)
            unit_of_work.add_outbox(
                OutboxMessage(
                    event_id=self._new_id(),
                    event_type=EVENT_TYPE,
                    schema_version=SCHEMA_VERSION,
                    aggregate_type=AGGREGATE_TYPE,
                    aggregate_id=plan.plan_id,
                    aggregate_version=plan.version,
                    occurred_at=now,
                    correlation_id=command.correlation_id,
                    causation_id=str(command.idempotency_key),
                    payload=nutrition_plan_created_payload(plan),
                )
            )
            unit_of_work.add_idempotency(
                IdempotencyRecord(
                    key=command.idempotency_key,
                    operation=OPERATION,
                    request_fingerprint=fingerprint,
                    resource_id=plan.plan_id,
                    response=creation_document(plan, warnings),
                    created_at=now,
                    expires_at=now + IDEMPOTENCY_TTL,
                )
            )
            unit_of_work.commit()
        return CreatedNutritionPlan(plan=plan, warnings=warnings)
