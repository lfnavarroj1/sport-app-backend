"""HU037: ingestión de lotes de frecuencia cardiaca (ADR-044 §5-§7, CA3-CA6).

1. Solo `athlete`; validar la forma del lote (`session_id`, `device_id`, 1 a 60 muestras).
2. Verificar `training.realtime.monitor` en Afiliación **fuera** de la transacción.
3. En una transacción, con la sesión y el dispositivo bloqueados en modo compartido:
   - sesión inexistente o ajena → `training_session_not_found`; no `ACTIVE` →
     `training_session_already_finished`;
   - dispositivo no vinculado al deportista → `device_not_found`; `REVOKED` → `device_revoked`;
   - validar cada muestra contra el inicio de la sesión y el reloj del backend (CA3) y guardar
     las válidas que no existan (`session_id`, `device_id`, `measured_at`) (CA4).
4. Responder, después del COMMIT, recibidas, aceptadas, duplicadas y rechazadas (CA5).

Sin `Idempotency-Key`: reenviar el mismo lote solo suma duplicados (idempotencia natural). Los
valores de frecuencia cardiaca nunca se registran en logs. La evaluación de riesgo, el resumen
`medicion.recibida.v1` y la retención de 90 días son de HU036.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID

from training_management.application.access import (
    require_athlete,
    require_realtime_monitor_capability,
)
from training_management.application.errors import (
    CapabilitiesUnavailable,
    CapabilityRequired,
    PermissionRequired,
    TrainingSessionAlreadyFinished,
    TrainingSessionNotFound,
)
from training_management.application.ports import (
    Caller,
    CapabilityChecker,
    Clock,
    MeasurementUnitOfWork,
)
from training_management.domain.devices.model import DeviceNotFound, DeviceRevoked
from training_management.domain.measurements.model import (
    InvalidMeasurementBatch,
    MeasurementBatch,
    RejectedSample,
    check_samples,
    first_occurrences,
    validate_batch,
)
from training_management.domain.sessions.model import TrainingSessionStatus

_audit = logging.getLogger("sportapp.audit")

_REJECTIONS = (
    PermissionRequired,
    CapabilityRequired,
    CapabilitiesUnavailable,
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


@dataclass(frozen=True)
class IngestionResult:
    session_id: UUID
    device_id: str
    received: int
    accepted: int
    duplicates: int
    rejected: tuple[RejectedSample, ...]


class IngestMeasurements:
    def __init__(
        self,
        *,
        unit_of_work: Callable[[], MeasurementUnitOfWork],
        capabilities: CapabilityChecker,
        clock: Clock,
    ) -> None:
        self._unit_of_work = unit_of_work
        self._capabilities = capabilities
        self._clock = clock

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
            },
        )
        return result

    @staticmethod
    def _audit(caller: Caller, fields: dict[str, object]) -> None:
        # Solo conteos: ni valores, ni instantes, ni identidad del dispositivo (datos de salud).
        _audit.info(
            "audit.measurements_ingested",
            extra={"fields": {"athlete_id": str(caller.user_id), **fields}},
        )

    def _execute(self, caller: Caller, command: IngestMeasurementsCommand) -> IngestionResult:
        require_athlete(caller)
        batch = validate_batch(command.session_id, command.body)
        require_realtime_monitor_capability(caller, self._capabilities)
        return self._ingest(caller.user_id, batch)

    def _ingest(self, athlete_id: UUID, batch: MeasurementBatch) -> IngestionResult:
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
            check = check_samples(
                batch.samples, started_at=session.started_at, now=self._clock.now()
            )
            unique = first_occurrences(check.valid)
            accepted = (
                unit_of_work.add_measurements(batch.session_id, batch.device_id, unique)
                if unique
                else 0
            )
            unit_of_work.commit()
        return IngestionResult(
            session_id=batch.session_id,
            device_id=batch.device_id,
            received=len(batch.samples),
            accepted=accepted,
            duplicates=len(check.valid) - accepted,
            rejected=check.rejected,
        )
