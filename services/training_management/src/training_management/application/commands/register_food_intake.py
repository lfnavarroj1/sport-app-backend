"""HU043: registrar un consumo de alimentos y bebidas (ADR-037).

1. Solo `athlete` (opción base «Mi alimentación», sin capacidad de plan).
2. El catálogo de alimentos vigente se lee **fuera** de la transacción; se validan
   `Idempotency-Key` y el cuerpo (CA2): una entrada inválida no crea consumo, clave ni evento.
3. Clave por deportista y operación vigente (30 días): misma huella → respuesta original
   (`REPLAYED`); otra huella → `idempotency_key_reused` (CA3).
4. Un `intake_id` ya registrado con los mismos datos devuelve el consumo existente sin otro
   evento (`EXISTING`); con otros datos o de otra persona, `intake_id_reused` (CA3).
5. En una transacción: consumo, ítems, `consumo_alimento.registrado.v1` en la outbox y la
   respuesta idempotente (CA4). Las claves primarias serializan las carreras: quien pierde
   devuelve lo que confirmó el ganador.
La respuesta se emite después del COMMIT y no espera la proyección.
"""

import hashlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from uuid import UUID, uuid4

from training_management.application.access import require_athlete
from training_management.application.errors import (
    IdempotencyKeyConflict,
    IdempotencyKeyReused,
    IntakeIdConflict,
    IntakeIdReused,
    PermissionRequired,
)
from training_management.application.intake_documents import intake_from_document
from training_management.application.ports import (
    Caller,
    Clock,
    CommandRecord,
    FoodCatalogReader,
    FoodIntakeUnitOfWork,
    OutboxMessage,
)
from training_management.domain.nutrition.intake import (
    FoodIntake,
    IntakeData,
    InvalidFoodIntake,
    Issue,
    Violation,
    validate_food_intake,
)
from training_management.domain.nutrition.intake_events import (
    AGGREGATE_TYPE,
    EVENT_TYPE,
    SCHEMA_VERSION,
    food_intake_document,
    food_intake_registered_payload,
)

OPERATION = "register_food_intake"
IDEMPOTENCY_TTL = timedelta(days=30)
IDEMPOTENCY_HEADER = "Idempotency-Key"

_audit = logging.getLogger("sportapp.audit")

_REJECTIONS = (PermissionRequired, InvalidFoodIntake, IdempotencyKeyReused, IntakeIdReused)


class IntakeOutcome(StrEnum):
    CREATED = "created"
    REPLAYED = "replayed"
    EXISTING = "existing"


@dataclass(frozen=True)
class RegisterFoodIntakeCommand:
    body: object
    idempotency_key: str | None
    correlation_id: UUID | None = None


@dataclass(frozen=True)
class FoodIntakeResult:
    intake: FoodIntake
    outcome: IntakeOutcome


def _fingerprint(athlete_id: UUID, data: IntakeData) -> str:
    canonical = json.dumps([OPERATION, str(athlete_id), str(data.intake_id), data.fingerprint])
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _key(raw: str | None) -> tuple[UUID | None, list[Violation]]:
    if raw is None or not raw.strip():
        return None, [Violation(IDEMPOTENCY_HEADER, Issue.REQUIRED)]
    try:
        return UUID(raw.strip()), []
    except ValueError:
        return None, [Violation(IDEMPOTENCY_HEADER, Issue.INVALID_UUID)]


def _audit_event(caller: Caller, fields: dict[str, object]) -> None:
    # Alimentación es dato de salud: sin alimentos, cantidades, cuerpo ni token (ADR-037).
    _audit.info(
        "audit.food_intake_created",
        extra={"fields": {**fields, "athlete_id": str(caller.user_id)}},
    )


class RegisterFoodIntake:
    def __init__(
        self,
        *,
        unit_of_work: Callable[[], FoodIntakeUnitOfWork],
        catalog: FoodCatalogReader,
        clock: Clock,
        new_id: Callable[[], UUID] = uuid4,
    ) -> None:
        self._unit_of_work = unit_of_work
        self._catalog = catalog
        self._clock = clock
        self._new_id = new_id

    def __call__(self, caller: Caller, command: RegisterFoodIntakeCommand) -> FoodIntakeResult:
        try:
            result = self._execute(caller, command)
        except _REJECTIONS as error:
            _audit_event(
                caller,
                {"result": "rejected", "reason": getattr(error, "code", type(error).__name__)},
            )
            raise
        _audit_event(
            caller,
            {"result": result.outcome.value, "intake_id": str(result.intake.intake_id)},
        )
        return result

    def _execute(self, caller: Caller, command: RegisterFoodIntakeCommand) -> FoodIntakeResult:
        require_athlete(caller)
        # El catálogo es dato de referencia: se lee antes de abrir la transacción del comando.
        catalog = self._catalog.load()
        key, violations = _key(command.idempotency_key)
        if key is None:
            # La clave es inválida: se informan también las reglas del cuerpo.
            problems = violations
            try:
                validate_food_intake(command.body, catalog, self._clock.now())
            except InvalidFoodIntake as error:
                problems.extend(error.violations)
            raise InvalidFoodIntake(problems)
        data = validate_food_intake(command.body, catalog, self._clock.now())
        fingerprint = _fingerprint(caller.user_id, data)
        previous = self._previous(caller.user_id, key, fingerprint)
        if previous is not None:
            return previous
        existing = self._existing(caller.user_id, data)
        if existing is not None:
            return existing
        intake = FoodIntake.register(
            athlete_id=caller.user_id, data=data, catalog=catalog, now=self._clock.now()
        )
        try:
            return self._create(caller, intake, key, fingerprint, command.correlation_id)
        except (IdempotencyKeyConflict, IntakeIdConflict):
            # Otra solicitud confirmó primero: se responde lo que ella confirmó.
            previous = self._previous(caller.user_id, key, fingerprint)
            if previous is not None:
                return previous
            existing = self._existing(caller.user_id, data)
            if existing is None:
                raise
            return existing

    def _previous(self, athlete_id: UUID, key: UUID, fingerprint: str) -> FoodIntakeResult | None:
        with self._unit_of_work() as unit_of_work:
            record = unit_of_work.find_command(athlete_id, OPERATION, key)
        if record is None or record.expires_at <= self._clock.now():
            return None
        if record.request_fingerprint != fingerprint:
            raise IdempotencyKeyReused()
        return FoodIntakeResult(intake_from_document(record.response), IntakeOutcome.REPLAYED)

    def _existing(self, athlete_id: UUID, data: IntakeData) -> FoodIntakeResult | None:
        with self._unit_of_work() as unit_of_work:
            intake = unit_of_work.get_food_intake(data.intake_id)
        if intake is None:
            return None
        if not intake.matches(athlete_id, data):
            raise IntakeIdReused()
        return FoodIntakeResult(intake, IntakeOutcome.EXISTING)

    def _create(
        self,
        caller: Caller,
        intake: FoodIntake,
        key: UUID,
        fingerprint: str,
        correlation_id: UUID | None,
    ) -> FoodIntakeResult:
        now = intake.registered_at
        with self._unit_of_work() as unit_of_work:
            unit_of_work.add_food_intake(intake)
            unit_of_work.add_outbox(
                OutboxMessage(
                    event_id=self._new_id(),
                    event_type=EVENT_TYPE,
                    schema_version=SCHEMA_VERSION,
                    aggregate_type=AGGREGATE_TYPE,
                    aggregate_id=intake.intake_id,
                    aggregate_version=intake.version,
                    occurred_at=now,
                    correlation_id=correlation_id,
                    causation_id=str(key),
                    payload=food_intake_registered_payload(intake),
                )
            )
            existing = unit_of_work.find_command(caller.user_id, OPERATION, key)
            if existing is not None:
                if existing.expires_at > now:
                    raise IdempotencyKeyConflict()
                unit_of_work.delete_command(caller.user_id, OPERATION, key)
            unit_of_work.add_command(
                CommandRecord(
                    actor_id=caller.user_id,
                    operation=OPERATION,
                    key=key,
                    request_fingerprint=fingerprint,
                    resource_id=intake.intake_id,
                    response=food_intake_document(intake),
                    created_at=now,
                    expires_at=now + IDEMPOTENCY_TTL,
                )
            )
            unit_of_work.commit()
        return FoodIntakeResult(intake, IntakeOutcome.CREATED)
