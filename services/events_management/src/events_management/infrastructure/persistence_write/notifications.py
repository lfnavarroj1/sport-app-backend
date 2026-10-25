"""Base de escritura de las notificaciones de HU039 (ADR-045 §9-10).

- `SqlAlchemyNotificationUnitOfWork`: un lote (≤ 100) de `notification` con
  `INSERT … ON CONFLICT (dedupe_key) DO NOTHING RETURNING` (solo las insertadas reciben
  outbox) y las inscripciones del planificador con `FOR UPDATE … SKIP LOCKED`.
- Preferencias con bloqueo de fila y control optimista por `version`.
- Tokens FCM cifrados, reasignados por `token_hash`; resultados de envío por token.
"""

from collections.abc import Collection, Sequence
from datetime import datetime
from typing import ClassVar
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from events_management.application.errors import IdempotencyKeyConflict
from events_management.application.ports import (
    DueReminder,
    PreferencesConflict,
    PushTarget,
)
from events_management.domain.notifications.model import (
    DEFAULT_PREFERENCES,
    Notification,
    NotificationPreferences,
)
from events_management.domain.sport_events.model import EventStatus
from events_management.infrastructure.persistence_write.models import (
    DELIVERY_UNIQUE,
    IDEMPOTENCY_PRIMARY_KEY,
    EventRegistrationRow,
    EventRow,
    NotificationDeliveryRow,
    NotificationPreferenceRow,
    NotificationRow,
    PushTokenRow,
)
from events_management.infrastructure.persistence_write.unit_of_work import (
    _sport_event,
    _WriteUnitOfWork,
)
from events_management.infrastructure.token_cipher import TokenCipher, token_hash

PREFERENCE_PRIMARY_KEY = "pk_notification_preference"


def _preferences(row: NotificationPreferenceRow) -> NotificationPreferences:
    return NotificationPreferences(
        event_matches=row.event_matches,
        event_reminders=row.event_reminders,
        push_enabled=row.push_enabled,
        version=row.version,
    )


class SqlAlchemyNotificationUnitOfWork(_WriteUnitOfWork):
    def preferences(self, recipient_ids: Collection[UUID]) -> dict[UUID, NotificationPreferences]:
        if not recipient_ids:
            return {}
        rows = self.session.scalars(
            select(NotificationPreferenceRow).where(
                NotificationPreferenceRow.recipient_id.in_(list(recipient_ids))
            )
        )
        return {row.recipient_id: _preferences(row) for row in rows}

    def add_notifications(self, notifications: Sequence[Notification]) -> list[Notification]:
        statement = (
            insert(NotificationRow)
            .values(
                [
                    {
                        "notification_id": item.notification_id,
                        "recipient_id": item.recipient_id,
                        "type": item.type.value,
                        "priority": item.priority.value,
                        "event_id": item.event_id,
                        "title": item.title,
                        "body": item.body,
                        "dedupe_key": item.dedupe_key,
                        "created_at": item.created_at,
                    }
                    for item in notifications
                ]
            )
            .on_conflict_do_nothing(index_elements=[NotificationRow.dedupe_key])
            .returning(NotificationRow.notification_id)
        )
        inserted = set(self.session.scalars(statement))
        return [item for item in notifications if item.notification_id in inserted]

    def due_reminders(
        self, starts_from: datetime, starts_to: datetime, after: UUID | None, limit: int
    ) -> list[DueReminder]:
        statement = (
            select(EventRegistrationRow.registration_id, EventRegistrationRow.athlete_id, EventRow)
            .join(EventRow, EventRow.event_id == EventRegistrationRow.event_id)
            .where(
                EventRow.status == EventStatus.PUBLISHED.value,
                EventRow.starts_at >= starts_from,
                EventRow.starts_at <= starts_to,
            )
            .order_by(EventRegistrationRow.registration_id)
            .limit(limit)
            # Otra réplica del planificador salta las inscripciones que ya tiene tomadas.
            .with_for_update(of=EventRegistrationRow, skip_locked=True)
        )
        if after is not None:
            statement = statement.where(EventRegistrationRow.registration_id > after)
        return [
            DueReminder(registration_id=row[0], athlete_id=row[1], event=_sport_event(row[2]))
            for row in self.session.execute(statement)
        ]


class SqlAlchemyPreferencesUnitOfWork(_WriteUnitOfWork):
    _conflicts: ClassVar = {
        IDEMPOTENCY_PRIMARY_KEY: IdempotencyKeyConflict,
        PREFERENCE_PRIMARY_KEY: PreferencesConflict,
    }

    def lock_preferences(self, recipient_id: UUID) -> NotificationPreferences | None:
        row = self.session.get(NotificationPreferenceRow, recipient_id, with_for_update=True)
        return None if row is None else _preferences(row)

    def save_preferences(
        self, recipient_id: UUID, preferences: NotificationPreferences, updated_at: datetime
    ) -> None:
        values = {
            "event_matches": preferences.event_matches,
            "event_reminders": preferences.event_reminders,
            "push_enabled": preferences.push_enabled,
            "version": preferences.version,
            "updated_at": updated_at,
        }
        row = self.session.get(NotificationPreferenceRow, recipient_id)
        if row is None:
            self.session.add(NotificationPreferenceRow(recipient_id=recipient_id, **values))
            return
        for name, value in values.items():
            setattr(row, name, value)


class SqlAlchemyPreferencesReader:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def preferences(self, recipient_id: UUID) -> NotificationPreferences:
        with self._session_factory() as session:
            row = session.get(NotificationPreferenceRow, recipient_id)
            return DEFAULT_PREFERENCES if row is None else _preferences(row)


class SqlAlchemyPushTokenStore:
    def __init__(self, session_factory: sessionmaker[Session], cipher: TokenCipher) -> None:
        self._session_factory = session_factory
        self._cipher = cipher

    def register(self, recipient_id: UUID, token: str, platform: str, now: datetime) -> None:
        statement = insert(PushTokenRow).values(
            token_id=uuid4(),
            recipient_id=recipient_id,
            token_hash=token_hash(token),
            token_ciphertext=self._cipher.encrypt(token),
            platform=platform,
            created_at=now,
            updated_at=now,
        )
        # El mismo dispositivo con otra sesión: el token pasa al deportista actual.
        statement = statement.on_conflict_do_update(
            index_elements=[PushTokenRow.token_hash],
            set_={
                "recipient_id": statement.excluded.recipient_id,
                "token_ciphertext": statement.excluded.token_ciphertext,
                "platform": statement.excluded.platform,
                "updated_at": statement.excluded.updated_at,
            },
        )
        with self._session_factory() as session, session.begin():
            session.execute(statement)


class SqlAlchemyPushDeliveryStore:
    def __init__(self, session_factory: sessionmaker[Session], cipher: TokenCipher) -> None:
        self._session_factory = session_factory
        self._cipher = cipher

    def push_enabled(self, recipient_id: UUID) -> bool:
        with self._session_factory() as session:
            row = session.get(NotificationPreferenceRow, recipient_id)
            return (row or DEFAULT_PREFERENCES).push_enabled

    def targets(self, recipient_id: UUID) -> list[PushTarget]:
        with self._session_factory() as session:
            rows = session.execute(
                select(PushTokenRow.token_id, PushTokenRow.token_ciphertext)
                .where(PushTokenRow.recipient_id == recipient_id)
                .order_by(PushTokenRow.token_id)
            ).all()
        return [PushTarget(token_id=row[0], token=self._cipher.decrypt(row[1])) for row in rows]

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
        statement = insert(NotificationDeliveryRow).values(
            delivery_id=uuid4(),
            notification_id=notification_id,
            token_id=token_id,
            channel="FCM",
            status="SENT" if sent else "FAILED",
            attempts=attempts,
            last_error=None if error is None else error[:64],
            created_at=now,
            updated_at=now,
        )
        statement = statement.on_conflict_do_update(
            constraint=DELIVERY_UNIQUE,
            set_={
                "status": statement.excluded.status,
                "attempts": statement.excluded.attempts,
                "last_error": statement.excluded.last_error,
                "updated_at": statement.excluded.updated_at,
            },
        )
        with self._session_factory() as session, session.begin():
            session.execute(statement)
