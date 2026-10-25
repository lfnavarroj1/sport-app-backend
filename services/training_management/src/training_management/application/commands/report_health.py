"""HU042: reportar una molestia, incapacidad o lesión (ADR-037).

1. Solo `athlete` (opción base «Mi salud», sin capacidad de plan); la identidad sale del
   token.
2. Validar `Idempotency-Key` y el cuerpo: catálogos cerrados, rangos, fechas y
   `consent.version` igual a `HEALTH_DATA_CONSENT_VERSION` (CA2). Una entrada inválida no
   crea reporte, clave ni evento.
3. Clave por deportista y operación vigente (30 días): misma huella → resultado original
   (`REPLAYED`); otra huella → `idempotency_key_reused` (CA3).
4. Un `report_id` ya registrado con los mismos datos devuelve el reporte existente sin otro
   evento (`EXISTING`); con otros datos o de otra persona, `report_id_reused` (CA3).
5. En una transacción: `session_id` opcional que debe ser una sesión propia (si no,
   `training_session_not_found`), reporte `OPEN` versión 1, `reporte_salud.registrado.v1` en
   la outbox y el resultado idempotente (CA1, CA4). Las claves primarias serializan las
   carreras: quien pierde devuelve lo que confirmó el ganador.
La respuesta se emite después del COMMIT y no espera la proyección. La auditoría solo lleva
identificadores: nunca tipo, zona, severidad ni fechas del reporte (datos de salud).
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
    PermissionRequired,
    ReportIdConflict,
    ReportIdReused,
    TrainingSessionNotFound,
)
from training_management.application.health_documents import (
    health_report_document,
    health_report_from_document,
)
from training_management.application.ports import (
    Caller,
    Clock,
    CommandRecord,
    HealthReportUnitOfWork,
    OutboxMessage,
)
from training_management.domain.health.events import (
    AGGREGATE_TYPE,
    EVENT_TYPE,
    SCHEMA_VERSION,
    registered_payload,
)
from training_management.domain.health.model import (
    HealthReport,
    HealthReportData,
    validate_report,
)
from training_management.domain.health.validation import (
    InvalidHealthReport,
    Issue,
    Violation,
)

OPERATION = "report_health"
IDEMPOTENCY_TTL = timedelta(days=30)
IDEMPOTENCY_HEADER = "Idempotency-Key"

_audit = logging.getLogger("sportapp.audit")

_REJECTIONS = (
    PermissionRequired,
    InvalidHealthReport,
    IdempotencyKeyReused,
    ReportIdReused,
    TrainingSessionNotFound,
)


class ReportOutcome(StrEnum):
    CREATED = "created"
    REPLAYED = "replayed"
    EXISTING = "existing"


@dataclass(frozen=True)
class ReportHealthCommand:
    body: object
    idempotency_key: str | None
    correlation_id: UUID | None = None


@dataclass(frozen=True)
class HealthReportResult:
    report: HealthReport
    outcome: ReportOutcome


def _fingerprint(athlete_id: UUID, data: HealthReportData) -> str:
    canonical = json.dumps([OPERATION, str(athlete_id), str(data.report_id), data.fingerprint])
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _key(raw: str | None) -> tuple[UUID | None, list[Violation]]:
    if raw is None or not raw.strip():
        return None, [Violation(IDEMPOTENCY_HEADER, Issue.REQUIRED)]
    try:
        return UUID(raw.strip()), []
    except ValueError:
        return None, [Violation(IDEMPOTENCY_HEADER, Issue.INVALID_UUID)]


def _audit_event(caller: Caller, fields: dict[str, object]) -> None:
    # Sin tipo, zona, severidad, fechas, cuerpo ni token (ADR-037: datos de salud).
    _audit.info(
        "audit.health_report_created",
        extra={"fields": {**fields, "athlete_id": str(caller.user_id)}},
    )


class ReportHealth:
    def __init__(
        self,
        *,
        unit_of_work: Callable[[], HealthReportUnitOfWork],
        clock: Clock,
        consent_version: str,
        new_id: Callable[[], UUID] = uuid4,
    ) -> None:
        self._unit_of_work = unit_of_work
        self._clock = clock
        self._consent_version = consent_version
        self._new_id = new_id

    def __call__(self, caller: Caller, command: ReportHealthCommand) -> HealthReportResult:
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
            {"result": result.outcome.value, "report_id": str(result.report.report_id)},
        )
        return result

    def _execute(self, caller: Caller, command: ReportHealthCommand) -> HealthReportResult:
        require_athlete(caller)
        key, violations = _key(command.idempotency_key)
        now = self._clock.now()
        if key is None:
            # La clave es inválida: se informan también las reglas del cuerpo.
            problems = violations
            try:
                validate_report(command.body, now, self._consent_version)
            except InvalidHealthReport as error:
                problems.extend(error.violations)
            raise InvalidHealthReport(problems)
        data = validate_report(command.body, now, self._consent_version)
        fingerprint = _fingerprint(caller.user_id, data)
        previous = self._previous(caller.user_id, key, fingerprint)
        if previous is not None:
            return previous
        existing = self._existing(caller.user_id, data)
        if existing is not None:
            return existing
        try:
            return self._create(caller, data, key, fingerprint, command.correlation_id)
        except (IdempotencyKeyConflict, ReportIdConflict):
            # Otra solicitud confirmó primero: se responde lo que ella confirmó.
            previous = self._previous(caller.user_id, key, fingerprint)
            if previous is not None:
                return previous
            existing = self._existing(caller.user_id, data)
            if existing is None:
                raise
            return existing

    def _previous(self, athlete_id: UUID, key: UUID, fingerprint: str) -> HealthReportResult | None:
        with self._unit_of_work() as unit_of_work:
            record = unit_of_work.find_command(athlete_id, OPERATION, key)
        if record is None or record.expires_at <= self._clock.now():
            return None
        if record.request_fingerprint != fingerprint:
            raise IdempotencyKeyReused()
        return HealthReportResult(
            health_report_from_document(record.response), ReportOutcome.REPLAYED
        )

    def _existing(self, athlete_id: UUID, data: HealthReportData) -> HealthReportResult | None:
        with self._unit_of_work() as unit_of_work:
            report = unit_of_work.get_health_report(data.report_id)
        if report is None:
            return None
        if not report.matches(athlete_id, data):
            raise ReportIdReused()
        return HealthReportResult(report, ReportOutcome.EXISTING)

    def _create(
        self,
        caller: Caller,
        data: HealthReportData,
        key: UUID,
        fingerprint: str,
        correlation_id: UUID | None,
    ) -> HealthReportResult:
        report = HealthReport.register(athlete_id=caller.user_id, data=data, now=self._clock.now())
        now = report.reported_at
        with self._unit_of_work() as unit_of_work:
            if data.session_id is not None:
                session = unit_of_work.get_session(data.session_id)
                # Inexistente o ajena: la misma respuesta, sin revelar cuál.
                if session is None or session.athlete_id != caller.user_id:
                    raise TrainingSessionNotFound()
            unit_of_work.add_health_report(report)
            unit_of_work.add_outbox(
                OutboxMessage(
                    event_id=self._new_id(),
                    event_type=EVENT_TYPE,
                    schema_version=SCHEMA_VERSION,
                    aggregate_type=AGGREGATE_TYPE,
                    aggregate_id=report.report_id,
                    aggregate_version=report.version,
                    occurred_at=now,
                    correlation_id=correlation_id,
                    causation_id=str(key),
                    payload=registered_payload(report),
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
                    resource_id=report.report_id,
                    response=health_report_document(report),
                    created_at=now,
                    expires_at=now + IDEMPOTENCY_TTL,
                )
            )
            unit_of_work.commit()
        return HealthReportResult(report, ReportOutcome.CREATED)
