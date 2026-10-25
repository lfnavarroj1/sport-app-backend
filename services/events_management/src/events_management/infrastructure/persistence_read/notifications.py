"""Bandeja de notificaciones en la base de lectura (HU039, ADR-045 §10-11).

`page` lee las propias en orden `created_at DESC, notification_id DESC` (keyset con el índice
`(recipient_id, created_at, notification_id)`); `mark_read` fija `read_at` una sola vez y
devuelve la fila, o `None` si no pertenece al deportista.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select, tuple_, update
from sqlalchemy.orm import Session, sessionmaker

from events_management.application.ports import InboxItem, InboxPosition
from events_management.infrastructure.persistence_read.models import NotificationInboxViewRow


def _item(row: NotificationInboxViewRow) -> InboxItem:
    return InboxItem(
        notification_id=row.notification_id,
        type=row.type,
        priority=row.priority,
        event_id=row.event_id,
        title=row.title,
        body=row.body,
        created_at=row.created_at,
        read_at=row.read_at,
    )


class SqlAlchemyInboxReader:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def page(self, recipient_id: UUID, after: InboxPosition | None, limit: int) -> list[InboxItem]:
        statement = select(NotificationInboxViewRow).where(
            NotificationInboxViewRow.recipient_id == recipient_id
        )
        if after is not None:
            statement = statement.where(
                tuple_(
                    NotificationInboxViewRow.created_at, NotificationInboxViewRow.notification_id
                )
                < tuple_(after.created_at, after.notification_id)
            )
        statement = statement.order_by(
            NotificationInboxViewRow.created_at.desc(),
            NotificationInboxViewRow.notification_id.desc(),
        ).limit(limit)
        with self._session_factory() as session:
            return [_item(row) for row in session.scalars(statement)]

    def mark_read(
        self, recipient_id: UUID, notification_id: UUID, now: datetime
    ) -> InboxItem | None:
        owned = (
            NotificationInboxViewRow.notification_id == notification_id,
            NotificationInboxViewRow.recipient_id == recipient_id,
        )
        with self._session_factory() as session, session.begin():
            # Repetirlo conserva el primer `read_at` (idempotente).
            session.execute(
                update(NotificationInboxViewRow)
                .where(*owned)
                .values(read_at=func.coalesce(NotificationInboxViewRow.read_at, now))
            )
            row = session.scalar(select(NotificationInboxViewRow).where(*owned))
            return None if row is None else _item(row)
