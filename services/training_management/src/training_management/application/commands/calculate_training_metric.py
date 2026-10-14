"""HU034: calcular un indicador versionado (ADR-030).

1. Solo `athlete` con `training.metrics.ftp_vo2max`, verificada en Afiliación **antes y
   fuera** de la transacción (indisponible → `upstream_unavailable`; timeout →
   `upstream_timeout`).
2. Validar `Idempotency-Key` y el cuerpo contra el catálogo inyectado (CA3); una entrada
   inválida no crea resultado, clave ni evento.
3. Clave por deportista y operación vigente (30 días): misma huella → resultado original
   (`REPLAYED`); otra huella → `idempotency_key_reused` (CA4).
4. Un `calculation_id` ya registrado con los mismos datos devuelve el cálculo existente sin
   otro evento (`EXISTING`); con otros datos o de otra persona, `calculation_id_reused`.
5. En una transacción: cálculo, entradas, `indicador.calculado.v1` en la outbox y el
   resultado idempotente. Las claves primarias serializan las carreras: quien pierde
   devuelve el resultado que confirmó el ganador.
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

from training_management.application.access import require_metrics_capability
from training_management.application.errors import (
    CalculationIdConflict,
    CalculationIdReused,
    CapabilitiesUnavailable,
    CapabilityRequired,
    IdempotencyKeyConflict,
    IdempotencyKeyReused,
    PermissionRequired,
)
from training_management.application.metric_documents import (
    calculation_document,
    calculation_from_document,
)
from training_management.application.ports import (
    Caller,
    CapabilityChecker,
    Clock,
    CommandRecord,
    MetricUnitOfWork,
    OutboxMessage,
)
from training_management.domain.metrics.catalog import MetricCatalog
from training_management.domain.metrics.events import (
    AGGREGATE_TYPE,
    EVENT_TYPE,
    SCHEMA_VERSION,
    calculated_payload,
)
from training_management.domain.metrics.model import (
    CalculationData,
    TrainingMetricCalculation,
    ValidatedCalculation,
    validate_calculation,
)
from training_management.domain.metrics.validation import (
    InvalidMetricCalculation,
    Issue,
    Violation,
)

OPERATION = "calculate_training_metric"
IDEMPOTENCY_TTL = timedelta(days=30)
IDEMPOTENCY_HEADER = "Idempotency-Key"

_audit = logging.getLogger("sportapp.audit")

_REJECTIONS = (
    PermissionRequired,
    CapabilityRequired,
    CapabilitiesUnavailable,
    InvalidMetricCalculation,
    IdempotencyKeyReused,
    CalculationIdReused,
)


class CalculationOutcome(StrEnum):
    CREATED = "calculated"
    REPLAYED = "replayed"
    EXISTING = "existing"


@dataclass(frozen=True)
class CalculateTrainingMetricCommand:
    body: object
    idempotency_key: str | None
    correlation_id: UUID | None = None


@dataclass(frozen=True)
class CalculationResult:
    calculation: TrainingMetricCalculation
    outcome: CalculationOutcome


def _fingerprint(athlete_id: UUID, data: CalculationData) -> str:
    canonical = json.dumps([OPERATION, str(athlete_id), str(data.calculation_id), data.fingerprint])
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _key(raw: str | None) -> tuple[UUID | None, list[Violation]]:
    if raw is None or not raw.strip():
        return None, [Violation(IDEMPOTENCY_HEADER, Issue.REQUIRED)]
    try:
        return UUID(raw.strip()), []
    except ValueError:
        return None, [Violation(IDEMPOTENCY_HEADER, Issue.INVALID_UUID)]


def _audit_event(caller: Caller, fields: dict[str, object]) -> None:
    # Sin entradas, resultado, cuerpo ni token (ADR-030: datos sensibles).
    _audit.info(
        "audit.training_metric_calculated",
        extra={"fields": {**fields, "athlete_id": str(caller.user_id)}},
    )


class CalculateTrainingMetric:
    def __init__(
        self,
        *,
        unit_of_work: Callable[[], MetricUnitOfWork],
        capabilities: CapabilityChecker,
        catalog: MetricCatalog,
        clock: Clock,
        new_id: Callable[[], UUID] = uuid4,
    ) -> None:
        self._unit_of_work = unit_of_work
        self._capabilities = capabilities
        self._catalog = catalog
        self._clock = clock
        self._new_id = new_id

    def __call__(
        self, caller: Caller, command: CalculateTrainingMetricCommand
    ) -> CalculationResult:
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
            {
                "result": result.outcome.value,
                "calculation_id": str(result.calculation.calculation_id),
                "metric_code": result.calculation.metric_code,
            },
        )
        return result

    def _execute(
        self, caller: Caller, command: CalculateTrainingMetricCommand
    ) -> CalculationResult:
        # La capacidad se consulta antes de abrir la transacción (sin bloqueos durante HTTP).
        require_metrics_capability(caller, self._capabilities)
        key, violations = _key(command.idempotency_key)
        if key is None:
            # La clave es inválida: se informan también las reglas del cuerpo.
            problems = violations
            try:
                validate_calculation(command.body, self._catalog, self._clock.now())
            except InvalidMetricCalculation as error:
                problems.extend(error.violations)
            raise InvalidMetricCalculation(problems)
        validated = validate_calculation(command.body, self._catalog, self._clock.now())
        fingerprint = _fingerprint(caller.user_id, validated.data)
        previous = self._previous(caller.user_id, key, fingerprint)
        if previous is not None:
            return previous
        existing = self._existing(caller.user_id, validated.data)
        if existing is not None:
            return existing
        try:
            return self._create(caller, validated, key, fingerprint, command.correlation_id)
        except (IdempotencyKeyConflict, CalculationIdConflict):
            # Otra solicitud confirmó primero: se responde lo que ella confirmó.
            previous = self._previous(caller.user_id, key, fingerprint)
            if previous is not None:
                return previous
            existing = self._existing(caller.user_id, validated.data)
            if existing is None:
                raise
            return existing

    def _previous(self, athlete_id: UUID, key: UUID, fingerprint: str) -> CalculationResult | None:
        with self._unit_of_work() as unit_of_work:
            record = unit_of_work.find_command(athlete_id, OPERATION, key)
        if record is None or record.expires_at <= self._clock.now():
            return None
        if record.request_fingerprint != fingerprint:
            raise IdempotencyKeyReused()
        return CalculationResult(
            calculation_from_document(record.response), CalculationOutcome.REPLAYED
        )

    def _existing(self, athlete_id: UUID, data: CalculationData) -> CalculationResult | None:
        with self._unit_of_work() as unit_of_work:
            calculation = unit_of_work.get_calculation(data.calculation_id)
        if calculation is None:
            return None
        if not calculation.matches(athlete_id, data):
            raise CalculationIdReused()
        return CalculationResult(calculation, CalculationOutcome.EXISTING)

    def _create(
        self,
        caller: Caller,
        validated: ValidatedCalculation,
        key: UUID,
        fingerprint: str,
        correlation_id: UUID | None,
    ) -> CalculationResult:
        calculation = TrainingMetricCalculation.calculate(
            athlete_id=caller.user_id, validated=validated, now=self._clock.now()
        )
        now = calculation.calculated_at
        with self._unit_of_work() as unit_of_work:
            unit_of_work.add_calculation(calculation)
            unit_of_work.add_outbox(
                OutboxMessage(
                    event_id=self._new_id(),
                    event_type=EVENT_TYPE,
                    schema_version=SCHEMA_VERSION,
                    aggregate_type=AGGREGATE_TYPE,
                    aggregate_id=calculation.calculation_id,
                    aggregate_version=calculation.version,
                    occurred_at=now,
                    correlation_id=correlation_id,
                    causation_id=str(key),
                    payload=calculated_payload(calculation),
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
                    resource_id=calculation.calculation_id,
                    response=calculation_document(calculation),
                    created_at=now,
                    expires_at=now + IDEMPOTENCY_TTL,
                )
            )
            unit_of_work.commit()
        return CalculationResult(calculation, CalculationOutcome.CREATED)
