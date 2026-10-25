"""HU032: iniciar y finalizar una sesión de entrenamiento (ADR-029).

Inicio:
1. Solo `athlete` (CA1); validar `session_id`, plan, deporte e instante.
2. Clave de idempotencia por deportista y operación vigente (30 días): devolver el
   resultado previo o `idempotency_key_reused` (CA4).
3. Un `session_id` ya registrado con el mismo inicio devuelve la sesión vigente sin otro
   evento; con otros datos, `session_id_reused`.
4. Verificar `training.session.record` en Afiliación **fuera** de la transacción.
5. En una transacción: plan propio `ASSIGNED` con el deporte (CA9), sesión `ACTIVE`
   versión 1, `entrenamiento.iniciado.v1` en la outbox y el resultado idempotente. La
   restricción única parcial de la base serializa las carreras (CA5).

Finalización (sin consultar Afiliación):
1. Solo `athlete`; validar el instante; clave previa como en el inicio.
2. En una transacción con la sesión bloqueada: propia (`404` si no, CA3); si ya terminó
   con el mismo `ended_at` y el mismo motivo, devolverla sin otro evento, y con otro,
   `training_session_already_finished`; si no, `FINISHED` versión 2 con la duración
   calculada, `entrenamiento.finalizado.v1` y el resultado idempotente (CA2).
3. HU036 (ADR-044 §10): el motivo opcional `RISK_STOP` se guarda en `end_reason` y forma
   parte de la huella de la clave; las mediciones de la sesión se conservan. El payload de
   `entrenamiento.finalizado.v1` no cambia.
Las respuestas se emiten después del COMMIT y no esperan la proyección.
"""

import hashlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID, uuid4

from training_management.application.access import require_athlete
from training_management.application.errors import (
    ActiveSessionExists,
    CapabilitiesUnavailable,
    CapabilityRequired,
    IdempotencyKeyConflict,
    IdempotencyKeyReused,
    PermissionRequired,
    SessionIdConflict,
    SessionIdReused,
    TrainingPlanNotFound,
    TrainingSessionAlreadyFinished,
    TrainingSessionNotFound,
)
from training_management.application.ports import (
    Caller,
    CapabilityChecker,
    Clock,
    CommandRecord,
    OutboxMessage,
    SessionUnitOfWork,
)
from training_management.application.session_documents import (
    session_document,
    session_from_document,
)
from training_management.domain.sessions.events import (
    AGGREGATE_TYPE,
    EVENT_TYPE,
    SCHEMA_VERSION,
    STARTED_EVENT_TYPE,
    session_finished_payload,
    session_started_payload,
)
from training_management.domain.sessions.model import (
    FinishData,
    StartData,
    StartInput,
    TrainingSession,
    TrainingSessionStatus,
    validate_finish,
    validate_start,
)
from training_management.domain.sessions.validation import InvalidTrainingSession, Violation

START = "start_training_session"
FINISH = "finish_training_session"
RECORD_CAPABILITY = "training.session.record"
IDEMPOTENCY_TTL = timedelta(days=30)

_audit = logging.getLogger("sportapp.audit")

_REJECTIONS = (
    PermissionRequired,
    CapabilityRequired,
    CapabilitiesUnavailable,
    InvalidTrainingSession,
    IdempotencyKeyReused,
    SessionIdReused,
    ActiveSessionExists,
    TrainingPlanNotFound,
    TrainingSessionNotFound,
    TrainingSessionAlreadyFinished,
)


@dataclass(frozen=True)
class StartTrainingSessionCommand:
    start: StartInput
    idempotency_key: UUID
    correlation_id: UUID | None = None


@dataclass(frozen=True)
class FinishTrainingSessionCommand:
    session_id: str
    ended_at: str | None
    idempotency_key: UUID
    transport_violations: tuple[Violation, ...] = ()
    correlation_id: UUID | None = None
    reason: str | None = None


@dataclass(frozen=True)
class SessionResult:
    session: TrainingSession
    replayed: bool = False


def _fingerprint(*parts: object) -> str:
    canonical = json.dumps([str(part) if part is not None else None for part in parts])
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _audit_event(name: str, caller: Caller, fields: dict[str, object]) -> None:
    # Sin cuerpos, deporte, tiempos, token ni ubicación (ADR-029, datos sensibles).
    _audit.info(name, extra={"fields": {**fields, "athlete_id": str(caller.user_id)}})


class _SessionCommand:
    operation: str

    def __init__(
        self,
        *,
        unit_of_work: Callable[[], SessionUnitOfWork],
        clock: Clock,
        new_id: Callable[[], UUID] = uuid4,
    ) -> None:
        self._unit_of_work = unit_of_work
        self._clock = clock
        self._new_id = new_id

    def _previous(self, athlete_id: UUID, key: UUID, fingerprint: str) -> SessionResult | None:
        with self._unit_of_work() as unit_of_work:
            record = unit_of_work.find_command(athlete_id, self.operation, key)
        if record is None or record.expires_at <= self._clock.now():
            return None
        if record.request_fingerprint != fingerprint:
            raise IdempotencyKeyReused()
        return SessionResult(session_from_document(record.response), replayed=True)

    def _record(
        self, unit_of_work: SessionUnitOfWork, session: TrainingSession, key: UUID, fingerprint: str
    ) -> None:
        now = session.updated_at
        existing = unit_of_work.find_command(session.athlete_id, self.operation, key)
        if existing is not None:
            if existing.expires_at > now:
                raise IdempotencyKeyConflict()
            unit_of_work.delete_command(session.athlete_id, self.operation, key)
        unit_of_work.add_command(
            CommandRecord(
                actor_id=session.athlete_id,
                operation=self.operation,
                key=key,
                request_fingerprint=fingerprint,
                resource_id=session.session_id,
                response=session_document(session),
                created_at=now,
                expires_at=now + IDEMPOTENCY_TTL,
            )
        )

    def _outbox(
        self,
        session: TrainingSession,
        event_type: str,
        payload: dict[str, object],
        key: UUID,
        correlation_id: UUID | None,
    ) -> OutboxMessage:
        return OutboxMessage(
            event_id=self._new_id(),
            event_type=event_type,
            schema_version=SCHEMA_VERSION,
            aggregate_type=AGGREGATE_TYPE,
            aggregate_id=session.session_id,
            aggregate_version=session.version,
            occurred_at=session.updated_at,
            correlation_id=correlation_id,
            causation_id=str(key),
            payload=payload,
        )


class StartTrainingSession(_SessionCommand):
    operation = START

    def __init__(
        self,
        *,
        unit_of_work: Callable[[], SessionUnitOfWork],
        capabilities: CapabilityChecker,
        clock: Clock,
        new_id: Callable[[], UUID] = uuid4,
    ) -> None:
        super().__init__(unit_of_work=unit_of_work, clock=clock, new_id=new_id)
        self._capabilities = capabilities

    def validate(self, caller: Caller, start: StartInput) -> StartData:
        """Actor y reglas del inicio, sin persistir ni consultar Afiliación."""
        require_athlete(caller)
        return validate_start(start, self._clock.now())

    def __call__(self, caller: Caller, command: StartTrainingSessionCommand) -> SessionResult:
        try:
            result = self._execute(caller, command)
        except _REJECTIONS as error:
            _audit_event(
                "audit.training_session_started",
                caller,
                {"result": "rejected", "reason": getattr(error, "code", type(error).__name__)},
            )
            raise
        _audit_event(
            "audit.training_session_started",
            caller,
            {
                "result": "replayed" if result.replayed else "started",
                "session_id": str(result.session.session_id),
            },
        )
        return result

    def _execute(self, caller: Caller, command: StartTrainingSessionCommand) -> SessionResult:
        data = self.validate(caller, command.start)
        fingerprint = _fingerprint(
            START,
            caller.user_id,
            data.session_id,
            data.plan_id,
            data.sport.value,
            data.started_at.isoformat(),
        )
        previous = self._previous(caller.user_id, command.idempotency_key, fingerprint)
        if previous is not None:
            return previous
        existing = self._existing(caller.user_id, data)
        if existing is not None:
            return existing
        # La capacidad se consulta antes de abrir la transacción (sin bloqueos durante HTTP).
        if not self._capabilities.has_capability(caller.user_id, RECORD_CAPABILITY):
            raise CapabilityRequired()
        session = TrainingSession.start(athlete_id=caller.user_id, data=data, now=self._clock.now())
        try:
            return self._create(session, command, fingerprint)
        except IdempotencyKeyConflict:
            previous = self._previous(caller.user_id, command.idempotency_key, fingerprint)
            if previous is None:
                raise
            return previous
        except SessionIdConflict:
            existing = self._existing(caller.user_id, data)
            if existing is None:
                raise
            return existing

    def _existing(self, athlete_id: UUID, data: StartData) -> SessionResult | None:
        with self._unit_of_work() as unit_of_work:
            session = unit_of_work.get_session(data.session_id)
        if session is None:
            return None
        if session.athlete_id != athlete_id or not session.matches_start(data):
            raise SessionIdReused()
        return SessionResult(session, replayed=True)

    def _create(
        self, session: TrainingSession, command: StartTrainingSessionCommand, fingerprint: str
    ) -> SessionResult:
        with self._unit_of_work() as unit_of_work:
            if session.plan_id is not None and not unit_of_work.plan_allows(
                session.plan_id, session.athlete_id, session.sport
            ):
                raise TrainingPlanNotFound()
            unit_of_work.add_session(session)
            unit_of_work.add_outbox(
                self._outbox(
                    session,
                    STARTED_EVENT_TYPE,
                    session_started_payload(session),
                    command.idempotency_key,
                    command.correlation_id,
                )
            )
            self._record(unit_of_work, session, command.idempotency_key, fingerprint)
            unit_of_work.commit()
        return SessionResult(session)


class FinishTrainingSession(_SessionCommand):
    operation = FINISH

    def validate(self, caller: Caller, command: FinishTrainingSessionCommand) -> FinishData:
        require_athlete(caller)
        return validate_finish(
            command.session_id,
            command.ended_at,
            command.transport_violations,
            self._clock.now(),
            command.reason,
        )

    def __call__(self, caller: Caller, command: FinishTrainingSessionCommand) -> SessionResult:
        try:
            result = self._execute(caller, command)
        except _REJECTIONS as error:
            _audit_event(
                "audit.training_session_finished",
                caller,
                {"result": "rejected", "reason": getattr(error, "code", type(error).__name__)},
            )
            raise
        _audit_event(
            "audit.training_session_finished",
            caller,
            {
                "result": "replayed" if result.replayed else "finished",
                "session_id": str(result.session.session_id),
            },
        )
        return result

    def _execute(self, caller: Caller, command: FinishTrainingSessionCommand) -> SessionResult:
        data = self.validate(caller, command)
        # Sin motivo, la huella es la misma que antes de HU036 (claves vigentes compatibles).
        parts: list[object] = [FINISH, caller.user_id, data.session_id, data.ended_at.isoformat()]
        if data.reason is not None:
            parts.append(data.reason.value)
        fingerprint = _fingerprint(*parts)
        previous = self._previous(caller.user_id, command.idempotency_key, fingerprint)
        if previous is not None:
            return previous
        try:
            return self._finish(caller, data, command, fingerprint)
        except IdempotencyKeyConflict:
            previous = self._previous(caller.user_id, command.idempotency_key, fingerprint)
            if previous is None:
                raise
            return previous

    def _finish(
        self,
        caller: Caller,
        data: FinishData,
        command: FinishTrainingSessionCommand,
        fingerprint: str,
    ) -> SessionResult:
        with self._unit_of_work() as unit_of_work:
            session = unit_of_work.get_session(data.session_id, for_update=True)
            if session is None or session.athlete_id != caller.user_id:
                raise TrainingSessionNotFound()
            if session.status is TrainingSessionStatus.FINISHED:
                if session.matches_finish(data.ended_at, data.reason):
                    return SessionResult(session, replayed=True)
                raise TrainingSessionAlreadyFinished()
            finished = session.finish(
                ended_at=data.ended_at, now=self._clock.now(), reason=data.reason
            )
            unit_of_work.update_session(finished)
            unit_of_work.add_outbox(
                self._outbox(
                    finished,
                    EVENT_TYPE,
                    session_finished_payload(finished),
                    command.idempotency_key,
                    command.correlation_id,
                )
            )
            self._record(unit_of_work, finished, command.idempotency_key, fingerprint)
            unit_of_work.commit()
        return SessionResult(finished)
