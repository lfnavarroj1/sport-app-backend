"""HU039 con PostgreSQL real (requiere `TEST_DATABASE_URL`): 1.000 destinatarios en lotes de
100 con su outbox, deduplicación por `dedupe_key`, preferencias, planificador con
`SKIP LOCKED` y ventana, bandeja keyset y lectura, tokens cifrados y FCM simulado fallando sin
tocar la bandeja (ADR-045)."""

import threading
import unittest
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
from sqlalchemy import delete, select, text
from support import EventDatabases, event_databases

from events_management.application.commands.generate_notifications import (
    GenerateEventMatchNotifications,
    GenerateEventReminders,
)
from events_management.application.commands.notification_settings import (
    ChangeNotificationPreferences,
    PreferencesChange,
)
from events_management.application.commands.push_delivery import DeliverPush
from events_management.application.messages import parse_sport_event_message
from events_management.application.ports import AthletePage, Caller
from events_management.application.projections.event_projection import EventsProjector
from events_management.application.queries.notifications import (
    ListMyNotifications,
    MarkNotificationRead,
)
from events_management.domain.notifications.disciplines import load_aliases
from events_management.domain.notifications.events import EVENT_TYPE
from events_management.domain.notifications.model import (
    Notification,
    NotificationPreferences,
    NotificationPriority,
    NotificationType,
    VersionConflict,
)
from events_management.domain.sport_events.model import SportEvent
from events_management.infrastructure.clock import SystemClock
from events_management.infrastructure.integrations.fcm import FcmPushSender
from events_management.infrastructure.persistence_read.events import (
    SqlAlchemyProjectionUnitOfWork,
)
from events_management.infrastructure.persistence_read.models import (
    NotificationInboxViewRow,
    ProcessedEventRow,
)
from events_management.infrastructure.persistence_read.notifications import SqlAlchemyInboxReader
from events_management.infrastructure.persistence_write.models import (
    EventRegistrationRow,
    EventRow,
    NotificationDeliveryRow,
    NotificationRow,
    OutboxEventRow,
    PushTokenRow,
)
from events_management.infrastructure.persistence_write.notifications import (
    SqlAlchemyNotificationUnitOfWork,
    SqlAlchemyPreferencesReader,
    SqlAlchemyPreferencesUnitOfWork,
    SqlAlchemyPushDeliveryStore,
    SqlAlchemyPushTokenStore,
)
from events_management.infrastructure.persistence_write.outbox import SqlAlchemyOutboxStore
from events_management.infrastructure.persistence_write.unit_of_work import _sport_event
from events_management.infrastructure.token_cipher import TokenCipher, token_hash
from events_management.workers.outbox_publisher import build_envelope, ordering_key

CIPHER = TokenCipher(bytes(range(32)))


class ListDirectory:
    def __init__(self, user_ids: Sequence[UUID]) -> None:
        self.user_ids = list(user_ids)
        self.calls = 0

    def search(
        self, *, sports: Sequence[str], city: str, country_code: str, cursor: str | None, limit: int
    ) -> AthletePage:
        self.calls += 1
        start = int(cursor) if cursor else 0
        following = start + limit
        return AthletePage(
            user_ids=tuple(self.user_ids[start:following]),
            next_cursor=str(following) if following < len(self.user_ids) else None,
        )


class NotificationsPersistenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.db: EventDatabases = self.enterContext(event_databases("hu039"))

    def event(self, event_id: UUID) -> SportEvent:
        with self.db.writes() as session:
            row = session.get(EventRow, event_id)
            assert row is not None
            return _sport_event(row)

    def generate(self, directory: ListDirectory) -> GenerateEventMatchNotifications:
        return GenerateEventMatchNotifications(
            unit_of_work=lambda: SqlAlchemyNotificationUnitOfWork(self.db.writes),
            directory=directory,
            aliases=load_aliases(),
            clock=SystemClock(),
        )

    def relay_and_project(self) -> int:
        """Lleva la outbox pendiente de notificaciones a la bandeja con el proyector real."""
        store = SqlAlchemyOutboxStore(self.db.writes)
        project = EventsProjector(
            unit_of_work=lambda: SqlAlchemyProjectionUnitOfWork(self.db.reads),
            clock=SystemClock(),
        )
        moved = 0
        while batch := store.claim_batch("test", 500, 30):
            for claimed in batch:
                project(parse_sport_event_message(build_envelope(claimed)))
                self.assertEqual(ordering_key(claimed), claimed.payload["recipient_id"])
                store.mark_published(claimed.event_id, "test")
                moved += 1
        return moved

    def test_thousand_recipients_in_batches_without_duplicates(self) -> None:
        event = self.event(self.db.publish_event())
        athletes = [uuid4() for _ in range(1000)]
        muted = athletes[:5]
        change = ChangeNotificationPreferences(
            unit_of_work=lambda: SqlAlchemyPreferencesUnitOfWork(self.db.writes),
            reader=SqlAlchemyPreferencesReader(self.db.writes),
            clock=SystemClock(),
        )
        for user_id in muted:
            change(Caller(user_id, "athlete"), PreferencesChange(False, True, True, 0))
        generate = self.generate(ListDirectory(athletes))

        first = generate(event, message_id=uuid4(), correlation_id=uuid4())
        repeated = generate(event, message_id=uuid4(), correlation_id=uuid4())

        self.assertEqual((first.generated, first.disabled), (995, 5))
        self.assertEqual((repeated.generated, repeated.duplicates), (0, 995))
        self.assertEqual(self.db.count(NotificationRow), 995)
        self.assertEqual(self.db.count(OutboxEventRow), 995)
        with self.db.writes() as session:
            keys = session.scalars(select(NotificationRow.dedupe_key)).all()
            outbox_types = set(session.scalars(select(OutboxEventRow.event_type)))
        self.assertEqual(len(set(keys)), 995)
        self.assertEqual(outbox_types, {EVENT_TYPE})
        self.assertTrue(
            all(key.startswith(f"EVENT_PUBLISHED_MATCH:{event.event_id}:") for key in keys)
        )

        self.assertEqual(self.relay_and_project(), 995)
        self.assertEqual(self.db.count(NotificationInboxViewRow, read=True), 995)
        sample = athletes[500]
        page = ListMyNotifications(SqlAlchemyInboxReader(self.db.reads))(
            Caller(sample, "athlete"), limit=20, cursor=None
        )
        self.assertEqual([item.event_id for item in page.items], [event.event_id])
        muted_page = ListMyNotifications(SqlAlchemyInboxReader(self.db.reads))(
            Caller(muted[0], "athlete"), limit=20, cursor=None
        )
        self.assertEqual(muted_page.items, ())

    def test_concurrent_deliveries_of_the_same_event_insert_once(self) -> None:
        event = self.event(self.db.publish_event())
        athletes = [uuid4() for _ in range(300)]
        errors: list[BaseException] = []

        def run() -> None:
            try:
                self.generate(ListDirectory(athletes))(
                    event, message_id=uuid4(), correlation_id=None
                )
            except BaseException as error:  # pragma: no cover - se informa abajo
                errors.append(error)

        threads = [threading.Thread(target=run) for _ in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        self.assertEqual(self.db.count(NotificationRow), 300)
        self.assertEqual(self.db.count(OutboxEventRow), 300)

    def register(self, event_id: UUID, athlete_id: UUID | None = None) -> UUID:
        now = datetime.now(UTC)
        registration_id = uuid4()
        with self.db.writes() as session, session.begin():
            session.add(
                EventRegistrationRow(
                    registration_id=registration_id,
                    event_id=event_id,
                    athlete_id=athlete_id or uuid4(),
                    status="REGISTERED",
                    registered_at=now,
                    version=1,
                    created_at=now,
                    updated_at=now,
                )
            )
        return registration_id

    def test_scheduler_window_edges_and_repeated_runs(self) -> None:
        fixed = datetime.now(UTC).replace(microsecond=0)

        class Clock:
            def now(self) -> datetime:
                return fixed

        def event_at(delta: timedelta) -> UUID:
            event_id = self.db.publish_event(starts_in=timedelta(days=1))
            with self.db.writes() as session, session.begin():
                session.execute(
                    text(
                        "UPDATE event SET starts_at = :starts,"
                        " ends_at = :starts + interval '3 hours'"
                        " WHERE event_id = :id"
                    ),
                    {"starts": fixed + delta, "id": event_id},
                )
            return event_id

        inside = [
            event_at(timedelta(hours=23, minutes=45)),
            event_at(timedelta(hours=24, minutes=15)),
        ]
        outside = [
            event_at(timedelta(hours=23, minutes=44, seconds=59)),
            event_at(timedelta(hours=24, minutes=15, seconds=1)),
        ]
        expected = {self.register(event_id) for event_id in inside for _ in range(60)}
        for event_id in outside:
            self.register(event_id)
        generate = GenerateEventReminders(
            unit_of_work=lambda: SqlAlchemyNotificationUnitOfWork(self.db.writes), clock=Clock()
        )

        first = generate()
        second = generate()

        self.assertEqual(first.generated, len(expected))
        self.assertEqual((second.generated, second.duplicates), (0, len(expected)))
        with self.db.writes() as session:
            rows = session.scalars(select(NotificationRow)).all()
            causes = set(session.scalars(select(OutboxEventRow.causation_id)))
        self.assertEqual({row.type for row in rows}, {"EVENT_REMINDER_24H"})
        self.assertEqual({row.priority for row in rows}, {"HIGH"})
        self.assertEqual(causes, {str(item) for item in expected})

    def test_scheduler_skips_registrations_locked_by_another_replica(self) -> None:
        fixed = datetime.now(UTC)

        class Clock:
            def now(self) -> datetime:
                return fixed

        event_id = self.db.publish_event(starts_in=timedelta(days=1))
        locked = self.register(event_id)
        free = self.register(event_id)
        generate = GenerateEventReminders(
            unit_of_work=lambda: SqlAlchemyNotificationUnitOfWork(self.db.writes), clock=Clock()
        )
        with self.db.writes() as other, other.begin():
            other.execute(
                select(EventRegistrationRow)
                .where(EventRegistrationRow.registration_id == locked)
                .with_for_update()
            )
            result = generate()
        self.assertEqual(result.generated, 1)
        with self.db.writes() as session:
            causes = set(session.scalars(select(OutboxEventRow.causation_id)))
        self.assertEqual(causes, {str(free)})
        self.assertEqual(generate().generated, 1)  # liberado, la corrida siguiente lo toma

    def test_inbox_keyset_and_read_mark(self) -> None:
        event = self.event(self.db.publish_event())
        athlete = uuid4()
        other_events = [self.event(self.db.publish_event()) for _ in range(4)]
        for item in (event, *other_events):
            self.generate(ListDirectory([athlete]))(item, message_id=uuid4(), correlation_id=None)
        self.relay_and_project()
        caller = Caller(athlete, "athlete")
        list_notifications = ListMyNotifications(SqlAlchemyInboxReader(self.db.reads))

        pages = []
        cursor = None
        while True:
            page = list_notifications(caller, limit=2, cursor=cursor)
            pages.append(page)
            cursor = page.next_cursor
            if cursor is None:
                break
        items = [item for page in pages for item in page.items]
        self.assertEqual(len(items), 5)
        self.assertEqual(
            [(item.created_at, item.notification_id) for item in items],
            sorted(((item.created_at, item.notification_id) for item in items), reverse=True),
        )

        mark = MarkNotificationRead(SqlAlchemyInboxReader(self.db.reads), SystemClock())
        read = mark(caller, items[0].notification_id)
        again = mark(caller, items[0].notification_id)
        self.assertIsNotNone(read.read_at)
        self.assertEqual(again.read_at, read.read_at)
        self.assertIsNone(
            SqlAlchemyInboxReader(self.db.reads).mark_read(
                uuid4(), items[0].notification_id, datetime.now(UTC)
            )
        )

    def test_preferences_optimistic_concurrency(self) -> None:
        reader = SqlAlchemyPreferencesReader(self.db.writes)
        change = ChangeNotificationPreferences(
            unit_of_work=lambda: SqlAlchemyPreferencesUnitOfWork(self.db.writes),
            reader=reader,
            clock=SystemClock(),
        )
        caller = Caller(uuid4(), "athlete")
        self.assertEqual(reader.preferences(caller.user_id).version, 0)

        updated = change(caller, PreferencesChange(False, False, True, 0))

        self.assertEqual(updated, NotificationPreferences(False, False, True, 1))
        self.assertEqual(reader.preferences(caller.user_id), updated)
        with self.assertRaises(VersionConflict) as raised:
            change(caller, PreferencesChange(True, True, True, 0))
        self.assertEqual(raised.exception.current_version, 1)

    def test_push_tokens_are_encrypted_and_reassigned(self) -> None:
        store = SqlAlchemyPushTokenStore(self.db.writes, CIPHER)
        first, second = uuid4(), uuid4()
        store.register(first, "dispositivo-sintetico", "android", datetime.now(UTC))
        store.register(second, "dispositivo-sintetico", "android", datetime.now(UTC))
        with self.db.writes() as session:
            rows = session.scalars(select(PushTokenRow)).all()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].recipient_id, second)
        self.assertEqual(rows[0].token_hash, token_hash("dispositivo-sintetico"))
        self.assertNotIn(b"dispositivo-sintetico", rows[0].token_ciphertext)
        self.assertEqual(CIPHER.decrypt(rows[0].token_ciphertext), "dispositivo-sintetico")

    def test_failing_fcm_records_result_and_leaves_inbox_intact(self) -> None:
        athlete = uuid4()
        SqlAlchemyPushTokenStore(self.db.writes, CIPHER).register(
            athlete, "dispositivo-sintetico", "android", datetime.now(UTC)
        )
        requests: list[httpx.Request] = []

        def failing(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(503, json={"error": {"status": "UNAVAILABLE"}})

        sender = FcmPushSender(
            httpx.Client(base_url="https://fcm.test", transport=httpx.MockTransport(failing)),
            project_id="sportapp-test",
            access_token=lambda: "acceso",
        )
        deliver = DeliverPush(
            store=SqlAlchemyPushDeliveryStore(self.db.writes, CIPHER),
            sender=sender,
            clock=SystemClock(),
            sleep=lambda _: None,
        )
        event = self.event(self.db.publish_event())
        self.generate(ListDirectory([athlete]))(event, message_id=uuid4(), correlation_id=None)
        project = EventsProjector(
            unit_of_work=lambda: SqlAlchemyProjectionUnitOfWork(self.db.reads),
            clock=SystemClock(),
            deliver_push=deliver,
        )
        store = SqlAlchemyOutboxStore(self.db.writes)
        for claimed in store.claim_batch("test", 10, 30):
            project(parse_sport_event_message(build_envelope(claimed)))

        self.assertEqual(len(requests), 3)
        self.assertEqual(self.db.count(NotificationInboxViewRow, read=True), 1)
        with self.db.writes() as session:
            delivery = session.scalars(select(NotificationDeliveryRow)).one()
        self.assertEqual((delivery.status, delivery.attempts), ("FAILED", 3))
        self.assertEqual(delivery.last_error, "http_503")

    def test_rebuilding_the_inbox_does_not_push_again(self) -> None:
        """I-2 (CMP-004): vaciar la bandeja y `processed_event` y volver a proyectar recrea la
        notificación, pero el *push* ya entregado (`SENT`) no se repite."""
        athlete = uuid4()
        SqlAlchemyPushTokenStore(self.db.writes, CIPHER).register(
            athlete, "dispositivo-sintetico", "android", datetime.now(UTC)
        )
        requests: list[httpx.Request] = []

        def accepted(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json={"name": "projects/sportapp-test/messages/1"})

        deliver = DeliverPush(
            store=SqlAlchemyPushDeliveryStore(self.db.writes, CIPHER),
            sender=FcmPushSender(
                httpx.Client(base_url="https://fcm.test", transport=httpx.MockTransport(accepted)),
                project_id="sportapp-test",
                access_token=lambda: "acceso",
            ),
            clock=SystemClock(),
            sleep=lambda _: None,
        )
        event = self.event(self.db.publish_event())
        self.generate(ListDirectory([athlete]))(event, message_id=uuid4(), correlation_id=None)
        store = SqlAlchemyOutboxStore(self.db.writes)
        messages = [
            parse_sport_event_message(build_envelope(claimed))
            for claimed in store.claim_batch("test", 10, 30)
        ]
        self.assertEqual(len(messages), 1)
        project = EventsProjector(
            unit_of_work=lambda: SqlAlchemyProjectionUnitOfWork(self.db.reads),
            clock=SystemClock(),
            deliver_push=deliver,
        )
        project(messages[0])
        with self.db.reads() as session, session.begin():
            session.execute(delete(NotificationInboxViewRow))
            session.execute(delete(ProcessedEventRow))
        project(messages[0])

        self.assertEqual(len(requests), 1)
        self.assertEqual(self.db.count(NotificationInboxViewRow, read=True), 1)
        with self.db.writes() as session:
            delivery = session.scalars(select(NotificationDeliveryRow)).one()
        self.assertEqual((delivery.status, delivery.attempts), ("SENT", 1))

    def test_disabled_push_preference_sends_nothing(self) -> None:
        athlete = uuid4()
        SqlAlchemyPushTokenStore(self.db.writes, CIPHER).register(
            athlete, "dispositivo-sintetico", "android", datetime.now(UTC)
        )
        ChangeNotificationPreferences(
            unit_of_work=lambda: SqlAlchemyPreferencesUnitOfWork(self.db.writes),
            reader=SqlAlchemyPreferencesReader(self.db.writes),
            clock=SystemClock(),
        )(Caller(athlete, "athlete"), PreferencesChange(True, True, False, 0))
        sent: list[Any] = []

        class Sender:
            def send(self, token: str, data: dict[str, str]) -> None:
                sent.append(token)

        store = SqlAlchemyPushDeliveryStore(self.db.writes, CIPHER)
        self.assertFalse(store.push_enabled(athlete))
        self.assertEqual(
            [target.token for target in store.targets(athlete)], ["dispositivo-sintetico"]
        )
        event = self.event(self.db.publish_event())
        self.generate(ListDirectory([athlete]))(event, message_id=uuid4(), correlation_id=None)
        with self.db.writes() as session:
            row = session.scalars(select(NotificationRow)).one()
        notification = Notification(
            notification_id=row.notification_id,
            recipient_id=row.recipient_id,
            type=NotificationType(row.type),
            priority=NotificationPriority(row.priority),
            event_id=row.event_id,
            title=row.title,
            body=row.body,
            created_at=row.created_at,
        )
        DeliverPush(store=store, sender=Sender(), clock=SystemClock(), sleep=lambda _: None)(
            notification
        )
        self.assertEqual(sent, [])
        self.assertEqual(self.db.count(NotificationDeliveryRow), 0)


if __name__ == "__main__":
    unittest.main()
