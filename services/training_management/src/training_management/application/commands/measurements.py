"""HU037/HU036: ingestión y evaluación de lotes de frecuencia cardiaca (ADR-044 §5-§9).

1. Solo `athlete`; validar la forma del lote (`session_id`, `device_id`, 1 a 60 muestras).
2. Verificar `training.realtime.monitor` en Afiliación y resolver el umbral vigente (propio o
   por defecto con Usuarios) **fuera** de la transacción.
3. En una transacción, con la sesión y el dispositivo bloqueados en modo compartido y el
   estado del monitoreo de la sesión en exclusiva:
   - sesión inexistente o ajena → `training_session_not_found`; no `ACTIVE` →
     `training_session_already_finished`;
   - dispositivo no vinculado al deportista → `device_not_found`; `REVOKED` → `device_revoked`;
   - validar cada muestra contra el inicio de la sesión y el reloj del backend (CA3 de HU037)
     y guardar las válidas que no existan (`session_id`, `device_id`, `measured_at`);
   - HU036: evaluar `HR_ABOVE_THRESHOLD_V1` con las muestras del lote y las de los 30 s
     previos (`OK`, `RISK` o `NO_DATA`), registrar las alertas nuevas (como máximo una cada
     60 s) y, si hubo muestras nuevas, el resumen `medicion.recibida.v1`; cada alerta emite
     `riesgo.detectado.v1`. Todo en la outbox de la misma transacción.
4. Responder, después del COMMIT, recibidas, aceptadas, duplicadas, rechazadas y la
   evaluación (la alerta llega al móvil en esta respuesta, sin esperar la proyección).

Sin `Idempotency-Key`: reenviar el mismo lote solo suma duplicados y no repite resumen ni
alertas (idempotencia natural). Los valores de frecuencia cardiaca nunca se registran en logs.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid4

from training_management.application.access import (
    require_athlete,
    require_realtime_monitor_capability,
)
from training_management.application.errors import (
    AthleteContextUnavailable,
    CapabilitiesUnavailable,
    CapabilityRequired,
    PermissionRequired,
    TrainingSessionAlreadyFinished,
    TrainingSessionNotFound,
)
from training_management.application.monitoring_threshold import ResolveThreshold
from training_management.application.ports import (
    Caller,
    CapabilityChecker,
    Clock,
    MeasurementUnitOfWork,
    MonitoringState,
    OutboxMessage,
)
from training_management.domain.devices.model import DeviceNotFound, DeviceRevoked
from training_management.domain.measurements.model import (
    InvalidMeasurementBatch,
    Measurement,
    MeasurementBatch,
    RejectedSample,
    check_samples,
    first_occurrences,
    validate_batch,
)
from training_management.domain.monitoring.events import (
    AGGREGATE_TYPE,
    MEASUREMENT_EVENT_TYPE,
    RISK_EVENT_TYPE,
    SCHEMA_VERSION,
    measurement_payload,
    risk_payload,
)
from training_management.domain.monitoring.model import (
    ALERT_REPEAT,
    Evaluation,
    EvaluationStatus,
    RiskAlert,
    Threshold,
    evaluate,
    summarize,
    window_start,
)
from training_management.domain.sessions.model import TrainingSessionStatus

_audit = logging.getLogger("sportapp.audit")

_REJECTIONS = (
    PermissionRequired,
    CapabilityRequired,
    CapabilitiesUnavailable,
    AthleteContextUnavailable,
    InvalidMeasurementBatch,
    TrainingSessionNotFound,
    TrainingSessionAlreadyFinished,
    DeviceNotFound,
    DeviceRevoked,
)


@dataclass(frozen=True)
class IngestMeasurementsCommand:
    session_id: str
    body: object
    correlation_id: UUID | None = None


@dataclass(frozen=True)
class IngestionResult:
    session_id: UUID
    device_id: str
    received: int
    accepted: int
    duplicates: int
    rejected: tuple[RejectedSample, ...]
    evaluation: Evaluation


class IngestMeasurements:
    def __init__(
        self,
        *,
        unit_of_work: Callable[[], MeasurementUnitOfWork],
        capabilities: CapabilityChecker,
        thresholds: ResolveThreshold,
        clock: Clock,
        new_id: Callable[[], UUID] = uuid4,
    ) -> None:
        self._unit_of_work = unit_of_work
        self._capabilities = capabilities
        self._thresholds = thresholds
        self._clock = clock
        self._new_id = new_id

    def __call__(self, caller: Caller, command: IngestMeasurementsCommand) -> IngestionResult:
        try:
            result = self._execute(caller, command)
        except _REJECTIONS as error:
            reason = getattr(error, "code", type(error).__name__)
            self._audit(caller, {"result": "rejected", "reason": reason})
            raise
        self._audit(
            caller,
            {
                "result": "received",
                "session_id": str(result.session_id),
                "received": result.received,
                "accepted": result.accepted,
                "duplicates": result.duplicates,
                "rejected": len(result.rejected),
                "evaluation": result.evaluation.status.value,
                "alerts": len(result.evaluation.alerts),
            },
        )
        return result

    @staticmethod
    def _audit(caller: Caller, fields: dict[str, object]) -> None:
        # Solo conteos y el estado: ni valores, ni umbral, ni instantes, ni identidad del
        # dispositivo (datos de salud).
        _audit.info(
            "audit.measurements_ingested",
            extra={"fields": {"athlete_id": str(caller.user_id), **fields}},
        )

    def _execute(self, caller: Caller, command: IngestMeasurementsCommand) -> IngestionResult:
        require_athlete(caller)
        batch = validate_batch(command.session_id, command.body)
        require_realtime_monitor_capability(caller, self._capabilities)
        threshold = self._thresholds(caller.user_id)
        return self._ingest(caller.user_id, batch, threshold, command.correlation_id)

    def _ingest(
        self,
        athlete_id: UUID,
        batch: MeasurementBatch,
        threshold: Threshold,
        correlation_id: UUID | None,
    ) -> IngestionResult:
        with self._unit_of_work() as unit_of_work:
            session = unit_of_work.get_session_for_ingestion(batch.session_id)
            if session is None or session.athlete_id != athlete_id:
                raise TrainingSessionNotFound()
            if session.status is not TrainingSessionStatus.ACTIVE:
                raise TrainingSessionAlreadyFinished()
            device = unit_of_work.get_device_for_ingestion(athlete_id, batch.device_id)
            if device is None:
                raise DeviceNotFound()
            if not device.is_active:
                raise DeviceRevoked()
            now = self._clock.now()
            state = unit_of_work.lock_monitoring_state(batch.session_id, now)
            check = check_samples(batch.samples, started_at=session.started_at, now=now)
            unique = first_occurrences(check.valid)
            inserted = (
                unit_of_work.add_measurements(batch.session_id, batch.device_id, unique)
                if unique
                else ()
            )
            evaluation = self._evaluate(unit_of_work, batch, inserted, threshold.value, now)
            self._record(
                unit_of_work,
                batch=batch,
                inserted=inserted,
                evaluation=evaluation,
                state=state,
                now=now,
                correlation_id=correlation_id,
            )
            unit_of_work.commit()
        return IngestionResult(
            session_id=batch.session_id,
            device_id=batch.device_id,
            received=len(batch.samples),
            accepted=len(inserted),
            duplicates=len(check.valid) - len(inserted),
            rejected=check.rejected,
            evaluation=evaluation,
        )

    @staticmethod
    def _evaluate(
        unit_of_work: MeasurementUnitOfWork,
        batch: MeasurementBatch,
        inserted: tuple[Measurement, ...],
        threshold: int,
        now: datetime,
    ) -> Evaluation:
        latest = unit_of_work.latest_measured_at(batch.session_id, batch.device_id)
        if latest is None:
            return Evaluation(EvaluationStatus.NO_DATA, ())
        new_instants = [item.measured_at for item in inserted]
        since = window_start(new_instants, latest)
        samples = unit_of_work.window_samples(batch.session_id, batch.device_id, since)
        previous = unit_of_work.alert_instants(
            batch.session_id, since - ALERT_REPEAT, latest + ALERT_REPEAT
        )
        return evaluate(
            session_id=batch.session_id,
            device_id=batch.device_id,
            samples=samples,
            new_instants=new_instants,
            threshold=threshold,
            previous_alerts=previous,
            now=now,
        )

    def _record(
        self,
        unit_of_work: MeasurementUnitOfWork,
        *,
        batch: MeasurementBatch,
        inserted: tuple[Measurement, ...],
        evaluation: Evaluation,
        state: MonitoringState,
        now: datetime,
        correlation_id: UUID | None,
    ) -> None:
        if not inserted:
            return
        summary = summarize(
            batch.session_id,
            batch.device_id,
            [(item.measured_at, item.value) for item in inserted],
            state.summary_version + 1,
        )
        summary_event = self._outbox(
            MEASUREMENT_EVENT_TYPE,
            session_id=batch.session_id,
            version=summary.version,
            payload=measurement_payload(summary),
            now=now,
            correlation_id=correlation_id,
            causation_id=None,
        )
        unit_of_work.add_outbox(summary_event)
        alerts: tuple[RiskAlert, ...] = evaluation.alerts
        if alerts:
            unit_of_work.add_alerts(alerts, state.alert_count + 1, now)
        for offset, alert in enumerate(alerts, start=1):
            unit_of_work.add_outbox(
                self._outbox(
                    RISK_EVENT_TYPE,
                    session_id=batch.session_id,
                    version=state.alert_count + offset,
                    payload=risk_payload(alert),
                    now=now,
                    correlation_id=correlation_id,
                    # La alerta la causó el lote resumido en `medicion.recibida.v1`.
                    causation_id=str(summary_event.event_id),
                )
            )
        unit_of_work.save_monitoring_state(
            MonitoringState(
                session_id=batch.session_id,
                summary_version=summary.version,
                alert_count=state.alert_count + len(alerts),
            ),
            now,
        )

    def _outbox(
        self,
        event_type: str,
        *,
        session_id: UUID,
        version: int,
        payload: dict[str, object],
        now: datetime,
        correlation_id: UUID | None,
        causation_id: str | None,
    ) -> OutboxMessage:
        return OutboxMessage(
            event_id=self._new_id(),
            event_type=event_type,
            schema_version=SCHEMA_VERSION,
            aggregate_type=AGGREGATE_TYPE,
            aggregate_id=session_id,
            aggregate_version=version,
            occurred_at=now,
            correlation_id=correlation_id,
            causation_id=causation_id,
            payload=payload,
        )
