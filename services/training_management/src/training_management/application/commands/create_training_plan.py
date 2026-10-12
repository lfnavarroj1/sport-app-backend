"""HU010: crear y asignar un plan de entrenamiento.

Secuencia:
1. Verificar que quien llama tenga el permiso `training.plan.author` (CA2).
2. Validar la coherencia del plan sin tocar persistencia (CA3).
3. Si la clave de idempotencia ya tiene un resultado vigente, devolverlo (CA5).
4. En una sola transacción: comprobar la autorización activa del deportista para
   este profesional (CA2), guardar el plan asignado (CA1), registrar
   `plan.entrenamiento_creado.v1` en la outbox y el resultado idempotente (CA4, CA5).
La respuesta se emite después del COMMIT y no espera la proyección de lectura.
"""

import hashlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from uuid import UUID, uuid4

from training_management.application.access import require_plan_author
from training_management.application.errors import (
    AthleteAuthorizationRequired,
    IdempotencyKeyConflict,
    IdempotencyKeyReused,
    PermissionRequired,
)
from training_management.application.ports import (
    Caller,
    Clock,
    IdempotencyRecord,
    OutboxMessage,
    PermissionPolicy,
    PlanningUnitOfWork,
    PlanView,
)
from training_management.domain.plans.errors import InvalidTrainingPlan
from training_management.domain.plans.events import (
    AGGREGATE_TYPE,
    EVENT_TYPE,
    SCHEMA_VERSION,
    plan_created_payload,
)
from training_management.domain.plans.model import PlannedSession, Sport, TrainingPlan

OPERATION = "create_training_plan"
IDEMPOTENCY_TTL = timedelta(hours=24)

_audit = logging.getLogger("sportapp.audit")


@dataclass(frozen=True)
class SessionInput:
    scheduled_date: date
    sport: Sport
    duration_minutes: int
    intensity_rpe: int


@dataclass(frozen=True)
class CreateTrainingPlanCommand:
    athlete_id: UUID
    title: str
    objectives: tuple[str, ...]
    start_date: date
    end_date: date
    sessions: tuple[SessionInput, ...]
    idempotency_key: UUID
    correlation_id: UUID | None = None


@dataclass(frozen=True)
class CreatedPlan:
    plan: PlanView
    replayed: bool = False


def request_fingerprint(professional_id: UUID, plan: TrainingPlan) -> str:
    """Identifica la solicitud para detectar reutilización de la clave.

    Incluye al profesional: la clave de otro profesional nunca devuelve un plan ajeno.
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
                    session.scheduled_date.isoformat(),
                    session.sport.value,
                    session.duration_minutes,
                    session.intensity_rpe,
                ]
                for session in plan.sessions
            ],
        ],
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class CreateTrainingPlan:
    def __init__(
        self,
        *,
        unit_of_work: Callable[[], PlanningUnitOfWork],
        permissions: PermissionPolicy,
        clock: Clock,
        new_id: Callable[[], UUID] = uuid4,
    ) -> None:
        self._unit_of_work = unit_of_work
        self._permissions = permissions
        self._clock = clock
        self._new_id = new_id

    def __call__(self, caller: Caller, command: CreateTrainingPlanCommand) -> CreatedPlan:
        try:
            result = self._execute(caller, command)
        except (
            PermissionRequired,
            InvalidTrainingPlan,
            AthleteAuthorizationRequired,
            IdempotencyKeyReused,
        ) as error:
            _audit.info(
                "audit.training_plan_creation",
                extra={
                    "fields": {
                        "result": "rejected",
                        "reason": error.code,
                        "professional_id": str(caller.user_id),
                    }
                },
            )
            raise
        _audit.info(
            "audit.training_plan_creation",
            extra={
                "fields": {
                    "result": "replayed" if result.replayed else "created",
                    "plan_id": str(result.plan.plan_id),
                    "athlete_id": str(result.plan.athlete_id),
                    "professional_id": str(caller.user_id),
                }
            },
        )
        return result

    def _execute(self, caller: Caller, command: CreateTrainingPlanCommand) -> CreatedPlan:
        require_plan_author(caller, self._permissions)
        plan = TrainingPlan.create(
            plan_id=self._new_id(),
            athlete_id=command.athlete_id,
            professional_id=caller.user_id,
            title=command.title,
            objectives=list(command.objectives),
            start_date=command.start_date,
            end_date=command.end_date,
            sessions=[
                PlannedSession(
                    scheduled_date=item.scheduled_date,
                    sport=item.sport,
                    duration_minutes=item.duration_minutes,
                    intensity_rpe=item.intensity_rpe,
                )
                for item in command.sessions
            ],
            now=self._clock.now(),
        )
        fingerprint = request_fingerprint(caller.user_id, plan)

        previous = self._previous_result(command.idempotency_key, fingerprint)
        if previous is not None:
            return previous
        try:
            return self._create(command, plan, fingerprint)
        except IdempotencyKeyConflict:
            # Una solicitud concurrente con la misma clave confirmó primero: su resultado.
            previous = self._previous_result(command.idempotency_key, fingerprint)
            if previous is None:
                raise
            return previous

    def _previous_result(self, key: UUID, fingerprint: str) -> CreatedPlan | None:
        with self._unit_of_work() as unit_of_work:
            record = unit_of_work.find_idempotency(key)
        if record is None or record.expires_at <= self._clock.now():
            return None
        if record.operation != OPERATION or record.request_fingerprint != fingerprint:
            raise IdempotencyKeyReused()
        return CreatedPlan(PlanView.from_document(record.response), replayed=True)

    def _create(
        self, command: CreateTrainingPlanCommand, plan: TrainingPlan, fingerprint: str
    ) -> CreatedPlan:
        now = plan.created_at
        view = PlanView.from_plan(plan)
        with self._unit_of_work() as unit_of_work:
            existing = unit_of_work.find_idempotency(command.idempotency_key)
            if existing is not None:
                if existing.expires_at > now:
                    raise IdempotencyKeyConflict()
                unit_of_work.delete_idempotency(command.idempotency_key)
            if not unit_of_work.has_active_authorization(plan.athlete_id, plan.professional_id):
                raise AthleteAuthorizationRequired()
            unit_of_work.add_plan(plan)
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
                    payload=plan_created_payload(plan),
                )
            )
            unit_of_work.add_idempotency(
                IdempotencyRecord(
                    key=command.idempotency_key,
                    operation=OPERATION,
                    request_fingerprint=fingerprint,
                    resource_id=plan.plan_id,
                    response=view.to_document(),
                    created_at=now,
                    expires_at=now + IDEMPOTENCY_TTL,
                )
            )
            unit_of_work.commit()
        return CreatedPlan(view)
