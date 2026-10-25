"""HU039: preferencias de notificación y token *push* del deportista (ADR-045 §9, §11).

- `ChangeNotificationPreferences`: `event_matches`, `event_reminders` y `push_enabled` con
  `expected_version` (control optimista: un conflicto nunca se sobrescribe). Sin evento:
  solo las lee la generación en la base de escritura.
- `RegisterPushToken`: solo con `FCM_ENABLED`; el token se guarda cifrado y se reasigna si
  pertenecía a otro deportista.
Ambos solo para `athlete`; el deportista sale del token y no hay capacidad de plan.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass

from events_management.application.errors import PermissionRequired, PushNotEnabled
from events_management.application.ports import (
    Caller,
    Clock,
    PreferencesConflict,
    PreferencesReader,
    PreferencesUnitOfWork,
    PushTokenStore,
)
from events_management.domain.notifications.model import (
    DEFAULT_PREFERENCES,
    NotificationPreferences,
    VersionConflict,
    changed_preferences,
)

ATHLETE = "athlete"

_audit = logging.getLogger("sportapp.audit")


def require_athlete(caller: Caller) -> None:
    if caller.actor_type != ATHLETE:
        raise PermissionRequired()


@dataclass(frozen=True)
class PreferencesChange:
    event_matches: bool
    event_reminders: bool
    push_enabled: bool
    expected_version: int


class GetNotificationPreferences:
    def __init__(self, reader: PreferencesReader) -> None:
        self._reader = reader

    def __call__(self, caller: Caller) -> NotificationPreferences:
        require_athlete(caller)
        return self._reader.preferences(caller.user_id)


class ChangeNotificationPreferences:
    def __init__(
        self,
        *,
        unit_of_work: Callable[[], PreferencesUnitOfWork],
        reader: PreferencesReader,
        clock: Clock,
    ) -> None:
        self._unit_of_work = unit_of_work
        self._reader = reader
        self._clock = clock

    def __call__(self, caller: Caller, change: PreferencesChange) -> NotificationPreferences:
        require_athlete(caller)
        try:
            with self._unit_of_work() as unit_of_work:
                current = unit_of_work.lock_preferences(caller.user_id) or DEFAULT_PREFERENCES
                updated = changed_preferences(
                    current,
                    event_matches=change.event_matches,
                    event_reminders=change.event_reminders,
                    push_enabled=change.push_enabled,
                    expected_version=change.expected_version,
                )
                unit_of_work.save_preferences(caller.user_id, updated, self._clock.now())
                unit_of_work.commit()
        except PreferencesConflict:
            # Otra solicitud creó primero las preferencias: la versión esperada ya no rige.
            raise VersionConflict(self._reader.preferences(caller.user_id).version) from None
        _audit.info(
            "audit.notification_preferences_changed",
            extra={
                "fields": {"user_id": str(caller.user_id), "preferences_version": updated.version}
            },
        )
        return updated


class RegisterPushToken:
    def __init__(self, *, store: PushTokenStore | None, clock: Clock) -> None:
        # `None` = FCM deshabilitado (`FCM_ENABLED=false`): no hay tokens que guardar.
        self._store = store
        self._clock = clock

    def __call__(self, caller: Caller, token: str, platform: str) -> None:
        require_athlete(caller)
        if self._store is None:
            raise PushNotEnabled()
        self._store.register(caller.user_id, token, platform, self._clock.now())
        # Nunca se registra el token.
        _audit.info(
            "audit.push_token_registered",
            extra={"fields": {"user_id": str(caller.user_id), "platform": platform}},
        )
