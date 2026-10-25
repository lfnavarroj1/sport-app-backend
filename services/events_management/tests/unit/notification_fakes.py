"""Dobles en memoria para probar HU039 sin infraestructura."""

from collections.abc import Collection, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Self
from uuid import UUID, uuid4

from fakes import NOW, event_body

from events_management.api.payload import parse_event
from events_management.application.ports import (
    AthletePage,
    DueReminder,
    InboxItem,
    InboxPosition,
    OutboxMessage,
    PreferencesConflict,
    PushFailed,
    PushRejected,
    PushTarget,
    RecipientsUnavailable,
)
from events_management.domain.notifications.model import (
    DEFAULT_PREFERENCES,
    Notification,
    NotificationPreferences,
)
from events_management.domain.sport_events.model import SportEvent, validate_event


def published_event(**overrides: Any) -> SportEvent:
    draft = SportEvent.draft(
        event_id=uuid4(),
        organizer_id=uuid4(),
        details=validate_event(parse_event(event_body(**overrides)), NOW),
        now=NOW,
    )
    return draft.publish(NOW + timedelta(hours=1))


@dataclass
class NotificationStore:
    notifications: dict[str, Notification] = field(default_factory=dict)
    outbox: list[OutboxMessage] = field(default_factory=list)
    preferences: dict[UUID, NotificationPreferences] = field(default_factory=dict)
    registrations: list[DueReminder] = field(default_factory=list)
    commits: int = 0


class InMemoryNotificationUnitOfWork:
    def __init__(self, store: NotificationStore) -> None:
        self.store = store
        self._notifications: list[Notification] = []
        self._outbox: list[OutboxMessage] = []

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: Any) -> None:
        self._notifications, self._outbox = [], []

    def preferences(self, recipient_ids: Collection[UUID]) -> dict[UUID, NotificationPreferences]:
        return {
            user_id: self.store.preferences[user_id]
            for user_id in recipient_ids
            if user_id in self.store.preferences
        }

    def add_notifications(self, notifications: Sequence[Notification]) -> list[Notification]:
        pending = {item.dedupe_key for item in self._notifications}
        inserted = [
            item
            for item in notifications
            if item.dedupe_key not in self.store.notifications and item.dedupe_key not in pending
        ]
        self._notifications.extend(inserted)
        return inserted

    def due_reminders(
        self, starts_from: datetime, starts_to: datetime, after: UUID | None, limit: int
    ) -> list[DueReminder]:
        due = sorted(
            (
                item
                for item in self.store.registrations
                if starts_from <= item.event.details.starts_at <= starts_to
                and (after is None or item.registration_id > after)
            ),
            key=lambda item: item.registration_id,
        )
        return due[:limit]

    def add_outbox(self, message: OutboxMessage) -> None:
        self._outbox.append(message)

    def commit(self) -> None:
        for item in self._notifications:
            self.store.notifications[item.dedupe_key] = item
        self.store.outbox.extend(self._outbox)
        self.store.commits += 1


@dataclass
class FakeDirectory:
    """`POST /v1/users/athletes/search` con páginas de `user_ids` fijos."""

    user_ids: list[UUID]
    fail_on_call: int | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)

    def search(
        self, *, sports: Sequence[str], city: str, country_code: str, cursor: str | None, limit: int
    ) -> AthletePage:
        self.calls.append(
            {"sports": list(sports), "city": city, "country_code": country_code, "cursor": cursor}
        )
        if self.fail_on_call is not None and len(self.calls) == self.fail_on_call:
            raise RecipientsUnavailable()
        start = int(cursor) if cursor else 0
        page = self.user_ids[start : start + limit]
        following = start + limit
        return AthletePage(
            user_ids=tuple(page),
            next_cursor=str(following) if following < len(self.user_ids) else None,
        )


class InMemoryInbox:
    def __init__(self, items: dict[UUID, tuple[UUID, InboxItem]] | None = None) -> None:
        # notification_id → (recipient_id, item)
        self.items = items or {}

    def add(self, recipient_id: UUID, item: InboxItem) -> None:
        self.items[item.notification_id] = (recipient_id, item)

    def page(self, recipient_id: UUID, after: InboxPosition | None, limit: int) -> list[InboxItem]:
        own = sorted(
            (item for owner, item in self.items.values() if owner == recipient_id),
            key=lambda item: (item.created_at, item.notification_id),
            reverse=True,
        )
        if after is not None:
            own = [
                item
                for item in own
                if (item.created_at, item.notification_id)
                < (after.created_at, after.notification_id)
            ]
        return own[:limit]

    def mark_read(
        self, recipient_id: UUID, notification_id: UUID, now: datetime
    ) -> InboxItem | None:
        found = self.items.get(notification_id)
        if found is None or found[0] != recipient_id:
            return None
        item = found[1]
        if item.read_at is None:
            item = InboxItem(**{**item.__dict__, "read_at": now})
            self.items[notification_id] = (recipient_id, item)
        return item


class InMemoryPreferences:
    """Lector y unidad de trabajo de preferencias sobre el mismo diccionario."""

    def __init__(self) -> None:
        self.rows: dict[UUID, NotificationPreferences] = {}
        self.insert_race: NotificationPreferences | None = None
        self._pending: tuple[UUID, NotificationPreferences] | None = None

    def preferences(self, recipient_id: UUID) -> NotificationPreferences:
        return self.rows.get(recipient_id, DEFAULT_PREFERENCES)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: Any) -> None:
        self._pending = None

    def lock_preferences(self, recipient_id: UUID) -> NotificationPreferences | None:
        return self.rows.get(recipient_id)

    def save_preferences(
        self, recipient_id: UUID, preferences: NotificationPreferences, updated_at: datetime
    ) -> None:
        self._pending = (recipient_id, preferences)

    def commit(self) -> None:
        if self._pending is None:
            return
        recipient_id, preferences = self._pending
        if self.insert_race is not None and recipient_id not in self.rows:
            # Otra solicitud insertó primero la fila del deportista.
            self.rows[recipient_id] = self.insert_race
            raise PreferencesConflict()
        self.rows[recipient_id] = preferences


@dataclass
class InMemoryPushTokens:
    tokens: dict[str, tuple[UUID, str]] = field(default_factory=dict)

    def register(self, recipient_id: UUID, token: str, platform: str, now: datetime) -> None:
        self.tokens[token] = (recipient_id, platform)


@dataclass
class InMemoryDeliveries:
    enabled: bool = True
    targets_by_recipient: dict[UUID, list[PushTarget]] = field(default_factory=dict)
    records: list[dict[str, Any]] = field(default_factory=list)
    fail_reading: bool = False

    def push_enabled(self, recipient_id: UUID) -> bool:
        if self.fail_reading:
            raise RuntimeError("base de escritura caída")
        return self.enabled

    def targets(self, recipient_id: UUID) -> list[PushTarget]:
        return self.targets_by_recipient.get(recipient_id, [])

    def record(
        self,
        notification_id: UUID,
        token_id: UUID,
        *,
        sent: bool,
        attempts: int,
        error: str | None,
        now: datetime,
    ) -> None:
        self.records.append(
            {
                "notification_id": notification_id,
                "token_id": token_id,
                "sent": sent,
                "attempts": attempts,
                "error": error,
            }
        )


@dataclass
class ScriptedSender:
    """Responde, en orden, con cada resultado: `None` (entregado) o la excepción."""

    outcomes: list[Exception | None]
    sent: list[tuple[str, dict[str, str]]] = field(default_factory=list)

    def send(self, token: str, data: dict[str, str]) -> None:
        self.sent.append((token, data))
        outcome = self.outcomes.pop(0) if self.outcomes else None
        if outcome is not None:
            raise outcome


__all__ = [
    "NOW",
    "FakeDirectory",
    "InMemoryDeliveries",
    "InMemoryInbox",
    "InMemoryNotificationUnitOfWork",
    "InMemoryPreferences",
    "InMemoryPushTokens",
    "NotificationStore",
    "PushFailed",
    "PushRejected",
    "ScriptedSender",
    "published_event",
]
