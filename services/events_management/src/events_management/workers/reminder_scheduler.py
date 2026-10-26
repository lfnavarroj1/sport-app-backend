"""Planificador de recordatorios `EVENT_REMINDER_24H` (HU039, ADR-045 §8).

Ejecución: `python -m events_management.workers.reminder_scheduler`.

Cada `REMINDER_INTERVAL_SECONDS` (15 min) genera el recordatorio para las inscripciones cuyo
evento empieza en `[ahora + 23 h 45 min, ahora + 24 h 15 min]`. Las ventanas de corridas
consecutivas se solapan: el `dedupe_key` único y `FOR UPDATE SKIP LOCKED` hacen que una
corrida repetida o dos réplicas no dupliquen el recordatorio (CA3). Un fallo de la corrida
se registra y se reintenta en la siguiente.
"""

import logging
import signal
import threading

from sqlalchemy.orm import sessionmaker

from events_management.application.commands.generate_notifications import GenerateEventReminders
from events_management.infrastructure.clock import SystemClock
from events_management.infrastructure.persistence_write.notifications import (
    SqlAlchemyNotificationUnitOfWork,
)
from events_management.settings import ReminderSchedulerSettings
from sportapp_platform.config import load_settings
from sportapp_platform.database import create_database_engine
from sportapp_platform.logging import configure_logging

_logger = logging.getLogger("sportapp.notifications")


def run_forever(generate: GenerateEventReminders, interval: float, stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            result = generate()
            _logger.info(
                "reminders.generated",
                extra={
                    "fields": {
                        "generated": result.generated,
                        "duplicates": result.duplicates,
                        "disabled": result.disabled,
                    }
                },
            )
        except Exception as error:
            _logger.warning(
                "reminders.cycle_failed", extra={"fields": {"error_type": type(error).__name__}}
            )
        stop.wait(interval)


def main() -> None:
    settings = load_settings(ReminderSchedulerSettings)
    configure_logging(
        service="events-management-reminder-scheduler",
        environment=settings.app_env,
        version=settings.service_version,
        level=settings.log_level,
    )
    engine = create_database_engine(settings.database_write_url, settings)
    generate = GenerateEventReminders(
        unit_of_work=lambda: SqlAlchemyNotificationUnitOfWork(sessionmaker(engine)),
        clock=SystemClock(),
    )
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    _logger.info(
        "reminders.started",
        extra={"fields": {"interval_seconds": settings.reminder_interval_seconds}},
    )
    try:
        run_forever(generate, settings.reminder_interval_seconds, stop)
    finally:
        engine.dispose()
        _logger.info("reminders.stopped")


if __name__ == "__main__":
    main()
