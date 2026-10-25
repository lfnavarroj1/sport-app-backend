"""HU036: retención de 90 días de las mediciones de frecuencia cardiaca (ADR-044 §7).

Ejecución única (pensada para un `CronJob` diario): `python -m
training_management.workers.measurement_retention`. Borra por lotes cortos, cada uno en su
propia transacción, las muestras de `training_measurement` con `measured_at` anterior a
ahora - 90 días, hasta que no quede ninguna. Las alertas, el estado del monitoreo y las
sesiones se conservan. Solo registra conteos.
"""

import logging
import sys
from datetime import datetime, timedelta

from sqlalchemy.orm import sessionmaker

from sportapp_platform.config import load_settings
from sportapp_platform.database import create_database_engine
from sportapp_platform.logging import configure_logging
from training_management.application.ports import Clock, MeasurementRetentionStore
from training_management.infrastructure.clock import SystemClock
from training_management.infrastructure.persistence_write.unit_of_work import (
    SqlAlchemyMeasurementRetention,
)
from training_management.settings import MeasurementRetentionSettings

RETENTION = timedelta(days=90)

_logger = logging.getLogger("sportapp.retention")


def retention_cutoff(now: datetime) -> datetime:
    return now - RETENTION


def purge_expired(store: MeasurementRetentionStore, clock: Clock, batch_size: int) -> int:
    """Borra todas las muestras vencidas en lotes de `batch_size` y devuelve el total."""
    cutoff = retention_cutoff(clock.now())
    total = 0
    while True:
        deleted = store.purge_measurements(cutoff, batch_size)
        total += deleted
        if deleted < batch_size:
            return total


def main() -> None:
    settings = load_settings(MeasurementRetentionSettings)
    configure_logging(
        service="training-management-measurement-retention",
        environment=settings.app_env,
        version=settings.service_version,
        level=settings.log_level,
    )
    engine = create_database_engine(settings.database_write_url, settings)
    store = SqlAlchemyMeasurementRetention(sessionmaker(engine))
    try:
        purged = purge_expired(store, SystemClock(), settings.retention_batch_size)
    except Exception as error:
        _logger.error("retention.failed", extra={"fields": {"error_type": type(error).__name__}})
        sys.exit(1)
    finally:
        engine.dispose()
    _logger.info("retention.completed", extra={"fields": {"purged": purged}})


if __name__ == "__main__":
    main()
