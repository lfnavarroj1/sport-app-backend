"""HU038: `GET /v1/session-reminder-rules` y `GET /v1/me/session-reminder-preferences`
(ADR-045 §4-§5).

Solo `athlete` con `nutrition.session.reminders` (Afiliación antes de leer). Las reglas son el
ruleset versionado en código; las preferencias salen de `session_reminder_preference` en una
transacción corta o, sin fila, son los valores por defecto del ruleset con `version: 0`.
"""

from collections.abc import Callable

from training_management.application.access import require_session_reminders_capability
from training_management.application.ports import (
    Caller,
    CapabilityChecker,
    SessionReminderPreferencesUnitOfWork,
)
from training_management.domain.reminders.model import (
    RULESET,
    ReminderPreferences,
    ReminderRuleset,
)


class GetSessionReminderRules:
    def __init__(self, *, capabilities: CapabilityChecker) -> None:
        self._capabilities = capabilities

    def __call__(self, caller: Caller) -> ReminderRuleset:
        require_session_reminders_capability(caller, self._capabilities)
        return RULESET


class GetSessionReminderPreferences:
    def __init__(
        self,
        *,
        unit_of_work: Callable[[], SessionReminderPreferencesUnitOfWork],
        capabilities: CapabilityChecker,
    ) -> None:
        self._unit_of_work = unit_of_work
        self._capabilities = capabilities

    def __call__(self, caller: Caller) -> ReminderPreferences:
        require_session_reminders_capability(caller, self._capabilities)
        with self._unit_of_work() as unit_of_work:
            preferences = unit_of_work.get_preferences(caller.user_id)
        return preferences or ReminderPreferences.default(caller.user_id)
