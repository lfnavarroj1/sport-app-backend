"""HU030: publicar y retirar rutinas alimenticias sugeribles (ADR-041).

Mismo patrón que las plantillas de plan de HU028:

Publicación (`POST /v1/nutrition-routine-templates`, CA4):
1. Permiso por actor existente `nutrition.plan.author` (solo `service_provider`).
2. Leer el catálogo de alimentos vigente (HU014/HU043) fuera de cualquier transacción y validar la
   rutina con las reglas del agregado: `food_code` inexistente (`unknown_value`) o incompatible
   con el patrón declarado (`conflicts_with_restriction`) es `validation_error` con todos los
   detalles y nada se publica.
3. Clave de idempotencia por profesional y operación, vigente 24 horas: el reintento devuelve el
   resultado original; la misma clave con otra rutina es `idempotency_key_reused`.
4. En una transacción: rutina `PUBLISHED` versión 1, `plantilla_rutina.publicada.v1` en la outbox
   y el resultado idempotente. Si una solicitud concurrente con la misma clave confirma primero,
   se devuelve su resultado.

Retiro (`POST /v1/nutrition-routine-templates/{template_id}/retire`, CA4), con la fila bloqueada:
- inexistente o de otro profesional → `template_not_found`, sin revelar cuál;
- ya retirada → se devuelve tal cual, sin otro evento (retiro idempotente);
- `expected_version` distinto de la vigente → `version_conflict` con la versión vigente;
- si no, `RETIRED` con la versión siguiente y `plantilla_rutina.retirada.v1`.
Publicar una rutina no asigna ningún plan de alimentación (HU014).
"""

import hashlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID, uuid4

from training_management.application.access import require_nutrition_plan_author
from training_management.application.errors import (
    IdempotencyKeyConflict,
    IdempotencyKeyReused,
    PermissionRequired,
)
from training_management.application.ports import (
    Caller,
    Clock,
    CommandRecord,
    FoodCatalogReader,
    OutboxMessage,
    PermissionPolicy,
    RoutineUnitOfWork,
)
from training_management.application.routine_documents import routine_from_document
from training_management.domain.routines.events import (
    AGGREGATE_TYPE,
    PUBLISHED_EVENT_TYPE,
    RETIRED_EVENT_TYPE,
    SCHEMA_VERSION,
    routine_document,
)
from training_management.domain.routines.model import (
    InvalidRoutine,
    NutritionRoutineTemplate,
    RoutineInput,
    RoutineNotFound,
    RoutineStatus,
    RoutineVersionConflict,
)

PUBLISH_OPERATION = "publish_nutrition_routine_template"
IDEMPOTENCY_TTL = timedelta(hours=24)
_FINGERPRINT_FIELDS = (
    "professional_id",
    "title",
    "description",
    "goal",
    "sport",
    "dietary_pattern",
    "contains_allergens",
    "duration_days",
    "daily_meals",
)

_audit = logging.getLogger("sportapp.audit")


@dataclass(frozen=True)
class PublishRoutineCommand:
    data: RoutineInput
    idempotency_key: UUID
    correlation_id: UUID | None = None


@dataclass(frozen=True)
class RetireRoutineCommand:
    template_id: UUID
    expected_version: int
    correlation_id: UUID | None = None


@dataclass(frozen=True)
class RoutineResult:
    routine: NutritionRoutineTemplate
    replayed: bool = False


def request_fingerprint(routine: NutritionRoutineTemplate) -> str:
    """Identifica la solicitud con los datos normalizados (sin identificador ni instantes)."""
    document = routine_document(routine)
    canonical = json.dumps(
        [PUBLISH_OPERATION, {key: document[key] for key in _FINGERPRINT_FIELDS}],
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _outbox(
    routine: NutritionRoutineTemplate,
    event_type: str,
    event_id: UUID,
    correlation_id: UUID | None,
    causation: str,
) -> OutboxMessage:
    return OutboxMessage(
        event_id=event_id,
        event_type=event_type,
        schema_version=SCHEMA_VERSION,
        aggregate_type=AGGREGATE_TYPE,
        aggregate_id=routine.template_id,
        aggregate_version=routine.version,
        occurred_at=routine.updated_at,
        correlation_id=correlation_id,
        causation_id=causation,
        payload=routine_document(routine),
    )


def _audit_result(name: str, caller: Caller, result: str, **fields: str) -> None:
    # Solo identificadores y el resultado: ni títulos, ni alimentos, ni alérgenos.
    _audit.info(
        name,
        extra={"fields": {"professional_id": str(caller.user_id), "result": result, **fields}},
    )


class PublishRoutine:
    def __init__(
        self,
        *,
        unit_of_work: Callable[[], RoutineUnitOfWork],
        permissions: PermissionPolicy,
        catalog: FoodCatalogReader,
        clock: Clock,
        new_id: Callable[[], UUID] = uuid4,
    ) -> None:
        self._unit_of_work = unit_of_work
        self._permissions = permissions
        self._catalog = catalog
        self._clock = clock
        self._new_id = new_id

    def __call__(self, caller: Caller, command: PublishRoutineCommand) -> RoutineResult:
        try:
            result = self._execute(caller, command)
        except (PermissionRequired, InvalidRoutine, IdempotencyKeyReused) as error:
            _audit_result("audit.routine_published", caller, "rejected", reason=error.code)
            raise
        _audit_result(
            "audit.routine_published",
            caller,
            "replayed" if result.replayed else "published",
            template_id=str(result.routine.template_id),
        )
        return result

    def _execute(self, caller: Caller, command: PublishRoutineCommand) -> RoutineResult:
        require_nutrition_plan_author(caller, self._permissions)
        routine = NutritionRoutineTemplate.publish(
            template_id=self._new_id(),
            professional_id=caller.user_id,
            data=command.data,
            catalog=self._catalog.load(),
            now=self._clock.now(),
        )
        fingerprint = request_fingerprint(routine)
        previous = self._previous(caller.user_id, command.idempotency_key, fingerprint)
        if previous is not None:
            return previous
        try:
            return self._publish(routine, command, fingerprint)
        except IdempotencyKeyConflict:
            previous = self._previous(caller.user_id, command.idempotency_key, fingerprint)
            if previous is None:
                raise
            return previous

    def _previous(self, professional_id: UUID, key: UUID, fingerprint: str) -> RoutineResult | None:
        with self._unit_of_work() as unit_of_work:
            record = unit_of_work.find_command(professional_id, PUBLISH_OPERATION, key)
        if record is None or record.expires_at <= self._clock.now():
            return None
        if record.request_fingerprint != fingerprint:
            raise IdempotencyKeyReused()
        return RoutineResult(routine=routine_from_document(record.response), replayed=True)

    def _publish(
        self, routine: NutritionRoutineTemplate, command: PublishRoutineCommand, fingerprint: str
    ) -> RoutineResult:
        now = routine.created_at
        owner, key = routine.professional_id, command.idempotency_key
        with self._unit_of_work() as unit_of_work:
            existing = unit_of_work.find_command(owner, PUBLISH_OPERATION, key)
            if existing is not None:
                if existing.expires_at > now:
                    raise IdempotencyKeyConflict()
                unit_of_work.delete_command(owner, PUBLISH_OPERATION, key)
            unit_of_work.add_routine(routine)
            unit_of_work.add_outbox(
                _outbox(
                    routine, PUBLISHED_EVENT_TYPE, self._new_id(), command.correlation_id, str(key)
                )
            )
            unit_of_work.add_command(
                CommandRecord(
                    actor_id=owner,
                    operation=PUBLISH_OPERATION,
                    key=key,
                    request_fingerprint=fingerprint,
                    resource_id=routine.template_id,
                    response=routine_document(routine),
                    created_at=now,
                    expires_at=now + IDEMPOTENCY_TTL,
                )
            )
            unit_of_work.commit()
        return RoutineResult(routine=routine)


class RetireRoutine:
    def __init__(
        self,
        *,
        unit_of_work: Callable[[], RoutineUnitOfWork],
        permissions: PermissionPolicy,
        clock: Clock,
        new_id: Callable[[], UUID] = uuid4,
    ) -> None:
        self._unit_of_work = unit_of_work
        self._permissions = permissions
        self._clock = clock
        self._new_id = new_id

    def __call__(self, caller: Caller, command: RetireRoutineCommand) -> RoutineResult:
        fields = {"template_id": str(command.template_id)}
        try:
            result = self._execute(caller, command)
        except (PermissionRequired, RoutineNotFound, RoutineVersionConflict) as error:
            _audit_result("audit.routine_retired", caller, "rejected", reason=error.code, **fields)
            raise
        _audit_result(
            "audit.routine_retired", caller, "replayed" if result.replayed else "retired", **fields
        )
        return result

    def _execute(self, caller: Caller, command: RetireRoutineCommand) -> RoutineResult:
        require_nutrition_plan_author(caller, self._permissions)
        with self._unit_of_work() as unit_of_work:
            routine = unit_of_work.get_routine_for_update(command.template_id)
            if routine is None or routine.professional_id != caller.user_id:
                raise RoutineNotFound()
            if routine.status is RoutineStatus.RETIRED:
                return RoutineResult(routine=routine, replayed=True)
            if routine.version != command.expected_version:
                raise RoutineVersionConflict(routine.version)
            retired = routine.retire(self._clock.now())
            unit_of_work.update_routine(retired)
            unit_of_work.add_outbox(
                _outbox(
                    retired,
                    RETIRED_EVENT_TYPE,
                    self._new_id(),
                    command.correlation_id,
                    str(retired.template_id),
                )
            )
            unit_of_work.commit()
        return RoutineResult(routine=retired)
