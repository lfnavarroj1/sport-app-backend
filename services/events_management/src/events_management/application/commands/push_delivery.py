"""HU039: envío *push* opcional por FCM después de proyectar la bandeja (ADR-045 §10).

Solo existe con `FCM_ENABLED`. El proyector lo invoca **después** del COMMIT de
`notification_inbox_view`, así que la bandeja nunca depende del resultado (CA4):
- sin `push_enabled` en las preferencias, o sin tokens registrados, no se envía nada (CA2);
- por cada token: hasta `MAX_ATTEMPTS` intentos ante fallos transitorios (respetando
  `Retry-After` con tope), sin reintentar un rechazo definitivo;
- el resultado (`SENT`/`FAILED`, intentos, causa) queda en `notification_delivery`;
- un token que ya recibió la notificación (`SENT`) no se vuelve a usar: reproyectar o
  reconstruir la bandeja no repite el *push* (CMP-004).
Cualquier error se registra y se absorbe: nunca impide confirmar el mensaje.
"""

import logging
from collections.abc import Callable

from events_management.application.ports import (
    Clock,
    PushDeliveryStore,
    PushFailed,
    PushRejected,
    PushSender,
)
from events_management.domain.notifications.model import Notification

MAX_ATTEMPTS = 3
BASE_BACKOFF_SECONDS = 0.5
MAX_BACKOFF_SECONDS = 2.0

_logger = logging.getLogger("sportapp.notifications")


def data_message(notification: Notification) -> dict[str, str]:
    """Mensaje de datos de FCM (solo cadenas): lo necesario para mostrar y navegar (HU041)."""
    return {
        "notification_id": str(notification.notification_id),
        "type": notification.type.value,
        "priority": notification.priority.value,
        "event_id": str(notification.event_id),
        "title": notification.title,
        "body": notification.body,
    }


class DeliverPush:
    def __init__(
        self,
        *,
        store: PushDeliveryStore,
        sender: PushSender,
        clock: Clock,
        sleep: Callable[[float], None],
    ) -> None:
        self._store = store
        self._sender = sender
        self._clock = clock
        self._sleep = sleep

    def __call__(self, notification: Notification) -> None:
        fields = {
            "notification_id": str(notification.notification_id),
            "recipient_id": str(notification.recipient_id),
        }
        try:
            if not self._store.push_enabled(notification.recipient_id):
                _logger.info("push.skipped", extra={"fields": {**fields, "reason": "disabled"}})
                return
            delivered = self._store.sent_token_ids(notification.notification_id)
            for target in self._store.targets(notification.recipient_id):
                if target.token_id in delivered:
                    continue
                sent, attempts, error = self._send(target.token, data_message(notification))
                self._store.record(
                    notification.notification_id,
                    target.token_id,
                    sent=sent,
                    attempts=attempts,
                    error=error,
                    now=self._clock.now(),
                )
                _logger.info(
                    "push.sent" if sent else "push.failed",
                    extra={
                        "fields": {
                            **fields,
                            "token_id": str(target.token_id),
                            "attempts": attempts,
                            "error": error,
                        }
                    },
                )
        except Exception as error:
            _logger.warning(
                "push.delivery_failed",
                extra={"fields": {**fields, "error_type": type(error).__name__}},
            )

    def _send(self, token: str, data: dict[str, str]) -> tuple[bool, int, str | None]:
        reason: str | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                self._sender.send(token, data)
            except PushRejected as rejected:
                return False, attempt, rejected.reason
            except PushFailed as failed:
                reason = failed.reason
                if attempt < MAX_ATTEMPTS:
                    backoff = failed.retry_after or BASE_BACKOFF_SECONDS * 2 ** (attempt - 1)
                    self._sleep(min(backoff, MAX_BACKOFF_SECONDS))
                continue
            return True, attempt, None
        return False, MAX_ATTEMPTS, reason
