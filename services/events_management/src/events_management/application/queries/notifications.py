"""HU039: bandeja de notificaciones del deportista (ADR-045 §10-11).

- `ListMyNotifications`: solo las propias desde `notification_inbox_view`, keyset
  `created_at DESC, notification_id DESC` con cursor opaco; `limit` de 1 a 50.
- `MarkNotificationRead`: fija `read_at` en la bandeja; repetirlo devuelve el mismo
  `read_at`. Una notificación ajena, inexistente o aún no proyectada es
  `notification_not_found`.
Solo `athlete`; sin capacidad de plan.
"""

import base64
import binascii
import json
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from events_management.application.commands.notification_settings import require_athlete
from events_management.application.errors import NotificationNotFound
from events_management.application.ports import (
    Caller,
    Clock,
    InboxItem,
    InboxPosition,
    InboxReader,
)

MAX_LIMIT = 50
DEFAULT_LIMIT = 20
CURSOR_MAX = 512


class InvalidNotificationsQuery(Exception):
    code = "validation_error"

    def __init__(self, field: str, issue: str) -> None:
        super().__init__(f"{field}: {issue}")
        self.field = field
        self.issue = issue


@dataclass(frozen=True)
class NotificationsPage:
    items: tuple[InboxItem, ...]
    next_cursor: str | None


def encode_cursor(position: InboxPosition) -> str:
    raw = json.dumps(
        {"t": position.created_at.isoformat(), "n": str(position.notification_id)},
        separators=(",", ":"),
    )
    return base64.urlsafe_b64encode(raw.encode("utf-8")).rstrip(b"=").decode("ascii")


def decode_cursor(cursor: str) -> InboxPosition:
    if len(cursor) > CURSOR_MAX:
        raise InvalidNotificationsQuery("cursor", "invalid_cursor")
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        document = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
        created_at = datetime.fromisoformat(document["t"])
        notification_id = UUID(document["n"])
    except (ValueError, KeyError, TypeError, binascii.Error, UnicodeError):
        raise InvalidNotificationsQuery("cursor", "invalid_cursor") from None
    if created_at.tzinfo is None:
        raise InvalidNotificationsQuery("cursor", "invalid_cursor")
    return InboxPosition(created_at=created_at, notification_id=notification_id)


class ListMyNotifications:
    def __init__(self, reader: InboxReader) -> None:
        self._reader = reader

    def __call__(self, caller: Caller, *, limit: int, cursor: str | None) -> NotificationsPage:
        require_athlete(caller)
        if not 1 <= limit <= MAX_LIMIT:
            raise InvalidNotificationsQuery("limit", "out_of_range")
        after = decode_cursor(cursor) if cursor else None
        # Una fila de más indica si hay otra página sin contar.
        rows = self._reader.page(caller.user_id, after, limit + 1)
        items = tuple(rows[:limit])
        next_cursor = (
            encode_cursor(InboxPosition(items[-1].created_at, items[-1].notification_id))
            if len(rows) > limit
            else None
        )
        return NotificationsPage(items=items, next_cursor=next_cursor)


class MarkNotificationRead:
    def __init__(self, reader: InboxReader, clock: Clock) -> None:
        self._reader = reader
        self._clock = clock

    def __call__(self, caller: Caller, notification_id: UUID) -> InboxItem:
        require_athlete(caller)
        item = self._reader.mark_read(caller.user_id, notification_id, self._clock.now())
        if item is None:
            raise NotificationNotFound()
        return item
