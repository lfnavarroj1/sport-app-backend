"""HU038: `PUT /v1/me/session-reminder-preferences` (ADR-045 §5).

1. Solo `athlete`; validar `{hydration, nutrition, expected_version}` con los rangos del
   ruleset `session-reminders-2026-10-13.v1`.
2. Verificar `nutrition.session.reminders` en Afiliación **fuera** de la transacción.
3. En una transacción con las preferencias bloqueadas: `expected_version` distinto de la
   vigente (`0` sin fila) → `version_conflict` con la vigente; mismos valores ya guardados → sin
   cambios (PUT idempotente); si no, crearlas (versión 1) o reemplazarlas (versión siguiente).
   Si otra solicitud concurrente las creó primero, se reintenta una vez sobre la fila ya
   existente (con `expected_version: 0` termina en `version_conflict`).

Sin evento: las preferencias no interesan a otros dominios. Los valores no se registran en los
logs: la auditoría solo lleva el resultado y la versión.
"""

import logging
from collections.abc import Callable

from training_management.application.access import (
    require_athlete,
    require_session_reminders_capability,
)
from training_management.application.errors import (
    CapabilitiesUnavailable,
    CapabilityRequired,
    PermissionRequired,
    SessionReminderPreferencesConflict,
)
from training_management.application.ports import (
    Caller,
    CapabilityChecker,
    Clock,
    SessionReminderPreferencesUnitOfWork,
)
from training_management.domain.reminders.model import (
    InvalidReminderPreferences,
    PreferencesChange,
    ReminderPreferences,
    ReminderPreferencesVersionConflict,
    validate_preferences,
)

_audit = logging.getLogger("sportapp.audit")

_REJECTIONS = (
    PermissionRequired,
    CapabilityRequired,
    CapabilitiesUnavailable,
    InvalidReminderPreferences,
    ReminderPreferencesVersionConflict,
)


class UpdateSessionReminderPreferences:
    def __init__(
        self,
        *,
        unit_of_work: Callable[[], SessionReminderPreferencesUnitOfWork],
        capabilities: CapabilityChecker,
        clock: Clock,
    ) -> None:
        self._unit_of_work = unit_of_work
        self._capabilities = capabilities
        self._clock = clock

    def __call__(self, caller: Caller, body: object) -> ReminderPreferences:
        try:
            preferences = self._execute(caller, body)
        except _REJECTIONS as error:
            self._audit(caller, {"result": "rejected", "reason": getattr(error, "code", "")})
            raise
        self._audit(caller, {"result": "updated", "preferences_version": preferences.version})
        return preferences

    @staticmethod
    def _audit(caller: Caller, fields: dict[str, object]) -> None:
        _audit.info(
            "audit.session_reminder_preferences_updated",
            extra={"fields": {"athlete_id": str(caller.user_id), **fields}},
        )

    def _execute(self, caller: Caller, body: object) -> ReminderPreferences:
        require_athlete(caller)
        change = validate_preferences(body)
        require_session_reminders_capability(caller, self._capabilities)
        try:
            return self._save(caller, change)
        except SessionReminderPreferencesConflict:
            return self._save(caller, change)

    def _save(self, caller: Caller, change: PreferencesChange) -> ReminderPreferences:
        now = self._clock.now()
        with self._unit_of_work() as unit_of_work:
            current = unit_of_work.get_preferences(caller.user_id, for_update=True)
            base = current or ReminderPreferences.default(caller.user_id)
            preferences = base.apply(change, now)
            if preferences is base:
                return base
            if current is None:
                unit_of_work.add_preferences(preferences)
            else:
                unit_of_work.update_preferences(preferences)
            unit_of_work.commit()
        return preferences
