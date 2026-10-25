"""HU039 sin infraestructura: reglas, generación, planificador, bandeja, preferencias, *push*
(FCM simulado), proyección y worker (ADR-045)."""

import json
import threading
import unittest
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
from fakes import FixedClock, InMemoryProjectionUnitOfWork, ReadStore
from fastapi.testclient import TestClient
from notification_fakes import (
    NOW,
    FakeDirectory,
    InMemoryDeliveries,
    InMemoryInbox,
    InMemoryNotificationUnitOfWork,
    InMemoryPreferences,
    InMemoryPushTokens,
    NotificationStore,
    PushFailed,
    PushRejected,
    ScriptedSender,
    published_event,
)
from pydantic import ValidationError
from sqlalchemy.exc import InterfaceError, OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from test_create_event import VERIFIER, token

from events_management.api.notifications import build_notifications_router
from events_management.application.commands.generate_notifications import (
    BATCH_SIZE,
    GenerateEventMatchNotifications,
    GenerateEventReminders,
)
from events_management.application.commands.notification_settings import (
    ChangeNotificationPreferences,
    GetNotificationPreferences,
    PreferencesChange,
    RegisterPushToken,
)
from events_management.application.commands.push_delivery import MAX_ATTEMPTS, DeliverPush
from events_management.application.errors import (
    NotificationNotFound,
    PermissionRequired,
    PushNotEnabled,
)
from events_management.application.messages import (
    InvalidEvent,
    NotificationRequestedMessage,
    parse_sport_event_message,
)
from events_management.application.ports import (
    Caller,
    DueReminder,
    InboxItem,
    PushTarget,
    RecipientsUnavailable,
    StoreUnavailable,
)
from events_management.application.projections.event_projection import (
    EventsProjector,
    ProjectionOutcome,
)
from events_management.application.queries.notifications import (
    InvalidNotificationsQuery,
    ListMyNotifications,
    MarkNotificationRead,
    decode_cursor,
)
from events_management.domain.notifications.disciplines import (
    load_aliases,
    normalize,
    sports_for_discipline,
)
from events_management.domain.notifications.events import requested_payload
from events_management.domain.notifications.model import (
    REMINDER_WINDOW_END,
    REMINDER_WINDOW_START,
    TITLE_MAX,
    Notification,
    NotificationPreferences,
    NotificationPriority,
    NotificationType,
    VersionConflict,
    changed_preferences,
    dedupe_key,
    new_notification,
    reminder_window,
)
from events_management.domain.sport_events.model import SportEvent
from events_management.domain.sport_events.published import aggregate_document
from events_management.infrastructure.database_errors import unavailable_on_failure
from events_management.infrastructure.integrations.fcm import FcmPushSender
from events_management.infrastructure.integrations.users import UsersAthleteDirectory
from events_management.infrastructure.persistence_read.notifications import SqlAlchemyInboxReader
from events_management.infrastructure.persistence_write.notifications import (
    SqlAlchemyPreferencesReader,
)
from events_management.infrastructure.persistence_write.outbox import ClaimedEvent
from events_management.infrastructure.token_cipher import (
    InvalidEncryptionKey,
    TokenCipher,
    decode_key,
    token_hash,
)
from events_management.settings import ReminderSchedulerSettings
from events_management.workers.notifications_worker import (
    NotificationMessageHandler,
    await_subscription,
)
from events_management.workers.outbox_publisher import ordering_key
from events_management.workers.read_projector import BackgroundPush
from sportapp_platform.app import create_service_app
from sportapp_platform.config import ServiceSettings

ALIASES = load_aliases()
MATCH = NotificationType.EVENT_PUBLISHED_MATCH
REMINDER = NotificationType.EVENT_REMINDER_24H


def notification(
    recipient_id: UUID | None = None,
    event: SportEvent | None = None,
    kind: NotificationType = MATCH,
) -> Notification:
    return new_notification(
        notification_id=uuid4(),
        notification_type=kind,
        event=event or published_event(),
        recipient_id=recipient_id or uuid4(),
        now=NOW,
    )


def requested_envelope(item: Notification, **overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "event_id": str(uuid4()),
        "event_type": "notificacion.solicitada.v1",
        "occurred_at": "2026-10-12T21:00:00Z",
        "producer": "events-management",
        "aggregate_type": "notification",
        "aggregate_id": str(item.notification_id),
        "aggregate_version": 1,
        "correlation_id": str(uuid4()),
        "causation_id": str(uuid4()),
        "schema_version": 1,
        "payload": requested_payload(item),
    }
    values.update(overrides)
    return values


class NotificationRulesTest(unittest.TestCase):
    def test_dedupe_key_is_type_event_and_recipient(self) -> None:
        event_id, recipient_id = uuid4(), uuid4()
        self.assertEqual(
            dedupe_key(REMINDER, event_id, recipient_id),
            f"EVENT_REMINDER_24H:{event_id}:{recipient_id}",
        )

    def test_priority_follows_type(self) -> None:
        self.assertIs(notification().priority, NotificationPriority.NORMAL)
        self.assertIs(notification(kind=REMINDER).priority, NotificationPriority.HIGH)

    def test_texts_have_no_personal_data_and_are_clipped(self) -> None:
        event = published_event(name="E" * 120)
        item = notification(event=event)
        self.assertEqual(len(item.title), TITLE_MAX)
        self.assertTrue(item.title.endswith("…"))
        self.assertIn("Bogotá", item.body)
        self.assertEqual(item.created_at.tzinfo, UTC)

    def test_preferences_decide_each_type(self) -> None:
        only_reminders = NotificationPreferences(event_matches=False, version=1)
        self.assertFalse(only_reminders.allows(MATCH))
        self.assertTrue(only_reminders.allows(REMINDER))
        self.assertTrue(NotificationPreferences().allows(MATCH))

    def test_changed_preferences_increment_version_or_conflict(self) -> None:
        current = NotificationPreferences(version=3)
        updated = changed_preferences(
            current,
            event_matches=False,
            event_reminders=True,
            push_enabled=False,
            expected_version=3,
        )
        self.assertEqual(updated.version, 4)
        self.assertFalse(updated.push_enabled)
        with self.assertRaises(VersionConflict) as raised:
            changed_preferences(
                current,
                event_matches=True,
                event_reminders=True,
                push_enabled=True,
                expected_version=2,
            )
        self.assertEqual(raised.exception.current_version, 3)

    def test_reminder_window_is_closed_between_23h45_and_24h15(self) -> None:
        start, end = reminder_window(NOW)
        self.assertEqual(start, NOW + timedelta(hours=23, minutes=45))
        self.assertEqual(end, NOW + timedelta(hours=24, minutes=15))


class DisciplineAliasesTest(unittest.TestCase):
    def test_discipline_maps_to_sports_by_normalized_alias(self) -> None:
        self.assertEqual(sports_for_discipline("  Media  Maratón ", ALIASES), ["running"])
        self.assertEqual(sports_for_discipline("NATACIÓN", ALIASES), ["swimming"])
        self.assertEqual(sports_for_discipline("MTB", ALIASES), ["cycling"])
        self.assertEqual(sports_for_discipline("Ajedrez", ALIASES), [])

    def test_other_has_no_aliases(self) -> None:
        self.assertNotIn("other", ALIASES)
        with self.assertRaises(ValueError):
            load_aliases({"version": "discipline-aliases.v1", "sports": {"other": ["x"]}})

    def test_table_is_validated(self) -> None:
        with self.assertRaises(ValueError):
            load_aliases({"version": "discipline-aliases.v0", "sports": {}})
        with self.assertRaises(ValueError):
            load_aliases({"version": "discipline-aliases.v1", "sports": {"running": ["Carrera"]}})
        self.assertEqual(normalize(" Ciclismo  de RUTA "), "ciclismo de ruta")


class GenerateEventMatchNotificationsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = NotificationStore()
        self.event = published_event(discipline="Carrera")
        self.athletes = [uuid4() for _ in range(250)]

    def generate(self, directory: FakeDirectory) -> GenerateEventMatchNotifications:
        return GenerateEventMatchNotifications(
            unit_of_work=lambda: InMemoryNotificationUnitOfWork(self.store),
            directory=directory,
            aliases=ALIASES,
            clock=FixedClock(NOW),
        )

    def test_one_notification_per_matching_athlete_in_batches_with_outbox(self) -> None:
        directory = FakeDirectory(self.athletes)
        message_id, correlation_id = uuid4(), uuid4()

        result = self.generate(directory)(
            self.event, message_id=message_id, correlation_id=correlation_id
        )

        self.assertEqual(result.generated, 250)
        self.assertEqual(self.store.commits, 3)  # lotes de 100, 100 y 50
        self.assertEqual(
            directory.calls[0],
            {"sports": ["running"], "city": "Bogotá", "country_code": "CO", "cursor": None},
        )
        recipients = {item.recipient_id for item in self.store.notifications.values()}
        self.assertEqual(recipients, set(self.athletes))
        self.assertEqual(len(self.store.outbox), 250)
        outbox = self.store.outbox[0]
        self.assertEqual(outbox.event_type, "notificacion.solicitada.v1")
        self.assertEqual(outbox.aggregate_type, "notification")
        self.assertEqual(outbox.aggregate_version, 1)
        self.assertEqual(outbox.ordering_key, outbox.payload["recipient_id"])
        self.assertEqual(outbox.causation_id, str(message_id))
        self.assertEqual(outbox.correlation_id, correlation_id)
        self.assertEqual(outbox.payload["type"], "EVENT_PUBLISHED_MATCH")
        self.assertEqual(outbox.payload["priority"], "NORMAL")
        self.assertEqual(outbox.payload["event_id"], str(self.event.event_id))

    def test_follows_every_search_page(self) -> None:
        athletes = [uuid4() for _ in range(2001)]
        directory = FakeDirectory(athletes)

        result = self.generate(directory)(self.event, message_id=uuid4(), correlation_id=None)

        self.assertEqual(result.generated, 2001)
        self.assertEqual([call["cursor"] for call in directory.calls], [None, "1000", "2000"])

    def test_repeated_delivery_does_not_duplicate(self) -> None:
        generate = self.generate(FakeDirectory(self.athletes))
        generate(self.event, message_id=uuid4(), correlation_id=None)

        again = generate(self.event, message_id=uuid4(), correlation_id=None)

        self.assertEqual(again.generated, 0)
        self.assertEqual(again.duplicates, 250)
        self.assertEqual(len(self.store.notifications), 250)
        self.assertEqual(len(self.store.outbox), 250)

    def test_disabled_matches_are_not_generated(self) -> None:
        muted = self.athletes[:10]
        for user_id in muted:
            self.store.preferences[user_id] = NotificationPreferences(
                event_matches=False, version=1
            )
        self.store.preferences[self.athletes[10]] = NotificationPreferences(
            event_reminders=False, version=1
        )

        result = self.generate(FakeDirectory(self.athletes))(
            self.event, message_id=uuid4(), correlation_id=None
        )

        self.assertEqual((result.generated, result.disabled), (240, 10))
        recipients = {item.recipient_id for item in self.store.notifications.values()}
        self.assertTrue(recipients.isdisjoint(muted))

    def test_users_failure_propagates_after_committed_batches(self) -> None:
        athletes = [uuid4() for _ in range(1500)]
        generate = self.generate(FakeDirectory(athletes, fail_on_call=2))

        with self.assertRaises(RecipientsUnavailable):
            generate(self.event, message_id=uuid4(), correlation_id=None)
        self.assertEqual(len(self.store.notifications), 1000)

        retry = self.generate(FakeDirectory(athletes))(
            self.event, message_id=uuid4(), correlation_id=None
        )
        self.assertEqual((retry.generated, retry.duplicates), (500, 1000))

    def test_discipline_without_sport_does_not_search(self) -> None:
        directory = FakeDirectory(self.athletes)
        event = published_event(discipline="Ajedrez")

        result = self.generate(directory)(event, message_id=uuid4(), correlation_id=None)

        self.assertEqual(result.generated, 0)
        self.assertEqual(directory.calls, [])


class GenerateEventRemindersTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = NotificationStore()
        self.clock = FixedClock(NOW)

    def reminder(self, starts_at: datetime, athlete_id: UUID | None = None) -> DueReminder:
        event = published_event(
            starts_at=starts_at.isoformat(), time_zone="UTC", discipline="Ciclismo"
        )
        return DueReminder(registration_id=uuid4(), athlete_id=athlete_id or uuid4(), event=event)

    def generate(self) -> GenerateEventReminders:
        return GenerateEventReminders(
            unit_of_work=lambda: InMemoryNotificationUnitOfWork(self.store), clock=self.clock
        )

    def test_window_edges_are_included_and_outside_excluded(self) -> None:
        inside = [
            self.reminder(NOW + timedelta(hours=23, minutes=45)),
            self.reminder(NOW + timedelta(hours=24)),
            self.reminder(NOW + timedelta(hours=24, minutes=15)),
        ]
        outside = [
            self.reminder(NOW + timedelta(hours=23, minutes=44, seconds=59)),
            self.reminder(NOW + timedelta(hours=24, minutes=15, seconds=1)),
        ]
        self.store.registrations = [*inside, *outside]

        result = self.generate()()

        self.assertEqual(result.generated, 3)
        recipients = {item.recipient_id for item in self.store.notifications.values()}
        self.assertEqual(recipients, {item.athlete_id for item in inside})
        self.assertTrue(all(item.type is REMINDER for item in self.store.notifications.values()))
        self.assertEqual(
            {message.causation_id for message in self.store.outbox},
            {str(item.registration_id) for item in inside},
        )

    def test_repeated_run_and_overlapping_window_do_not_duplicate(self) -> None:
        self.store.registrations = [self.reminder(NOW + timedelta(hours=24, minutes=10))]
        self.generate()()

        self.clock.advance(timedelta(minutes=15))
        again = self.generate()()

        self.assertEqual((again.generated, again.duplicates), (0, 1))
        self.assertEqual(len(self.store.outbox), 1)

    def test_more_than_one_batch_and_disabled_reminders(self) -> None:
        athletes = [uuid4() for _ in range(BATCH_SIZE + 5)]
        self.store.registrations = [
            self.reminder(NOW + timedelta(hours=24), athlete_id) for athlete_id in athletes
        ]
        self.store.preferences[athletes[0]] = NotificationPreferences(
            event_reminders=False, version=1
        )

        result = self.generate()()

        self.assertEqual((result.generated, result.disabled), (BATCH_SIZE + 4, 1))
        self.assertEqual(self.store.commits, 2)


def inbox_item(created_at: datetime, read_at: datetime | None = None) -> InboxItem:
    return InboxItem(
        notification_id=uuid4(),
        type="EVENT_PUBLISHED_MATCH",
        priority="NORMAL",
        event_id=uuid4(),
        title="Nuevo evento: Carrera",
        body="Running en Bogotá. Toca para ver el detalle.",
        created_at=created_at,
        read_at=read_at,
    )


class InboxQueriesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.athlete = Caller(uuid4(), "athlete")
        self.inbox = InMemoryInbox()
        self.items = [inbox_item(NOW + timedelta(minutes=minute)) for minute in range(5)]
        for item in self.items:
            self.inbox.add(self.athlete.user_id, item)
        self.inbox.add(uuid4(), inbox_item(NOW))

    def test_keyset_pages_newest_first_without_gaps(self) -> None:
        list_notifications = ListMyNotifications(self.inbox)
        first = list_notifications(self.athlete, limit=2, cursor=None)
        second = list_notifications(self.athlete, limit=2, cursor=first.next_cursor)
        third = list_notifications(self.athlete, limit=2, cursor=second.next_cursor)

        seen = [item.notification_id for page in (first, second, third) for item in page.items]
        expected = [item.notification_id for item in reversed(self.items)]
        self.assertEqual(seen, expected)
        self.assertIsNone(third.next_cursor)

    def test_same_instant_is_ordered_by_notification_id(self) -> None:
        caller = Caller(uuid4(), "athlete")
        twins = [inbox_item(NOW), inbox_item(NOW)]
        for item in twins:
            self.inbox.add(caller.user_id, item)
        list_notifications = ListMyNotifications(self.inbox)
        first = list_notifications(caller, limit=1, cursor=None)
        second = list_notifications(caller, limit=1, cursor=first.next_cursor)

        ids = sorted((item.notification_id for item in twins), reverse=True)
        self.assertEqual([first.items[0].notification_id, second.items[0].notification_id], ids)

    def test_limit_cursor_and_actor_are_validated(self) -> None:
        list_notifications = ListMyNotifications(self.inbox)
        for limit in (0, 51):
            with self.assertRaises(InvalidNotificationsQuery):
                list_notifications(self.athlete, limit=limit, cursor=None)
        with self.assertRaises(InvalidNotificationsQuery):
            decode_cursor("no-es-un-cursor")
        with self.assertRaises(PermissionRequired):
            list_notifications(Caller(uuid4(), "organizer"), limit=10, cursor=None)

    def test_mark_read_is_idempotent_and_owned(self) -> None:
        clock = FixedClock(NOW + timedelta(hours=1))
        mark = MarkNotificationRead(self.inbox, clock)
        target = self.items[0].notification_id

        first = mark(self.athlete, target)
        clock.advance(timedelta(hours=1))
        second = mark(self.athlete, target)

        self.assertEqual(first.read_at, NOW + timedelta(hours=1))
        self.assertEqual(second.read_at, first.read_at)
        with self.assertRaises(NotificationNotFound):
            mark(Caller(uuid4(), "athlete"), target)
        with self.assertRaises(NotificationNotFound):
            mark(self.athlete, uuid4())


class PreferencesAndPushTokenTest(unittest.TestCase):
    def setUp(self) -> None:
        self.preferences = InMemoryPreferences()
        self.athlete = Caller(uuid4(), "athlete")
        self.change = ChangeNotificationPreferences(
            unit_of_work=lambda: self.preferences,
            reader=self.preferences,
            clock=FixedClock(NOW),
        )

    def test_defaults_then_change_with_expected_version(self) -> None:
        current = GetNotificationPreferences(self.preferences)(self.athlete)
        self.assertEqual(current, NotificationPreferences(True, True, True, 0))

        updated = self.change(self.athlete, PreferencesChange(False, True, False, 0))

        self.assertEqual(updated, NotificationPreferences(False, True, False, 1))
        with self.assertRaises(VersionConflict) as raised:
            self.change(self.athlete, PreferencesChange(True, True, True, 0))
        self.assertEqual(raised.exception.current_version, 1)

    def test_concurrent_first_insert_is_a_version_conflict(self) -> None:
        self.preferences.insert_race = NotificationPreferences(True, False, True, 1)
        with self.assertRaises(VersionConflict) as raised:
            self.change(self.athlete, PreferencesChange(False, False, False, 0))
        self.assertEqual(raised.exception.current_version, 1)

    def test_only_athletes(self) -> None:
        with self.assertRaises(PermissionRequired):
            GetNotificationPreferences(self.preferences)(Caller(uuid4(), "organizer"))

    def test_push_token_requires_fcm(self) -> None:
        tokens = InMemoryPushTokens()
        disabled = RegisterPushToken(store=None, clock=FixedClock(NOW))
        with self.assertRaises(PushNotEnabled):
            disabled(self.athlete, "token-sintetico", "android")
        RegisterPushToken(store=tokens, clock=FixedClock(NOW))(
            self.athlete, "token-sintetico", "android"
        )
        self.assertEqual(tokens.tokens["token-sintetico"], (self.athlete.user_id, "android"))


class TokenCipherTest(unittest.TestCase):
    def test_round_trip_with_random_nonce(self) -> None:
        cipher = TokenCipher(bytes(range(32)))
        first, second = cipher.encrypt("dispositivo-1"), cipher.encrypt("dispositivo-1")
        self.assertNotEqual(first, second)
        self.assertNotIn(b"dispositivo-1", first)
        self.assertEqual(cipher.decrypt(first), "dispositivo-1")
        self.assertEqual(len(token_hash("dispositivo-1")), 64)

    def test_key_must_be_32_bytes_base64(self) -> None:
        self.assertEqual(decode_key("A" * 43), bytes(32))
        for invalid in ("corta", "@@@@"):
            with self.assertRaises(InvalidEncryptionKey):
                decode_key(invalid)


class DeliverPushTest(unittest.TestCase):
    def setUp(self) -> None:
        self.item = notification()
        self.target = PushTarget(token_id=uuid4(), token="token-sintetico")
        self.deliveries = InMemoryDeliveries(
            targets_by_recipient={self.item.recipient_id: [self.target]}
        )
        self.sleeps: list[float] = []

    def deliver(self, sender: ScriptedSender) -> None:
        DeliverPush(
            store=self.deliveries, sender=sender, clock=FixedClock(NOW), sleep=self.sleeps.append
        )(self.item)

    def test_sends_data_message_and_records_success(self) -> None:
        sender = ScriptedSender([None])
        self.deliver(sender)
        token_value, data = sender.sent[0]
        self.assertEqual(token_value, "token-sintetico")
        self.assertEqual(data["event_id"], str(self.item.event_id))
        self.assertEqual(data["priority"], "NORMAL")
        self.assertEqual(self.deliveries.records[0]["sent"], True)

    def test_transient_failures_are_retried_with_bound(self) -> None:
        sender = ScriptedSender([PushFailed("http_503", retry_after=30.0), PushFailed("timeout")])
        self.deliver(sender)
        self.assertEqual(len(sender.sent), 3)
        self.assertEqual(self.sleeps, [2.0, 1.0])  # Retry-After con tope; luego exponencial
        self.assertEqual(self.deliveries.records[0]["attempts"], 3)

        # Otra notificación: la anterior ya quedó `SENT` en ese token y no se reenvía (I-2).
        self.item = notification(recipient_id=self.item.recipient_id)
        exhausted = ScriptedSender([PushFailed("http_500")] * MAX_ATTEMPTS)
        self.deliver(exhausted)
        self.assertEqual(len(exhausted.sent), MAX_ATTEMPTS)
        self.assertEqual(
            self.deliveries.records[-1],
            {**self.deliveries.records[-1], "sent": False, "error": "http_500"},
        )

    def test_reprojection_does_not_push_again_to_delivered_tokens(self) -> None:
        """CMP-004: reconstruir la bandeja no repite el *push* ya entregado."""
        other = PushTarget(token_id=uuid4(), token="otro-dispositivo")
        self.deliveries.targets_by_recipient[self.item.recipient_id].append(other)
        self.deliver(ScriptedSender([None, PushFailed("http_503")] + [PushFailed("x")] * 2))
        again = ScriptedSender([None])
        self.deliver(again)
        self.assertEqual([token for token, _ in again.sent], ["otro-dispositivo"])

    def test_credential_failure_is_recorded(self) -> None:
        """M-1: un fallo de la cuenta de servicio queda en `notification_delivery`."""

        def broken() -> str:
            raise RuntimeError("credencial inválida")

        http = httpx.Client(
            base_url="https://fcm.test",
            transport=httpx.MockTransport(lambda _: httpx.Response(200)),
        )
        DeliverPush(
            store=self.deliveries,
            sender=FcmPushSender(http, project_id="p", access_token=broken),
            clock=FixedClock(NOW),
            sleep=self.sleeps.append,
        )(self.item)
        self.assertEqual(len(self.deliveries.records), 1)
        record = self.deliveries.records[0]
        self.assertEqual(
            (record["sent"], record["attempts"], record["error"]),
            (False, MAX_ATTEMPTS, "credential_error"),
        )

    def test_rejection_is_not_retried(self) -> None:
        sender = ScriptedSender([PushRejected("http_404")])
        self.deliver(sender)
        self.assertEqual(len(sender.sent), 1)
        self.assertEqual(self.deliveries.records[0]["error"], "http_404")

    def test_push_disabled_or_failing_store_never_raises(self) -> None:
        self.deliveries.enabled = False
        sender = ScriptedSender([])
        self.deliver(sender)
        self.assertEqual(sender.sent, [])

        self.deliveries.fail_reading = True
        self.deliver(sender)  # se registra y se absorbe
        self.assertEqual(self.deliveries.records, [])


class FcmPushSenderTest(unittest.TestCase):
    def sender(self, status: int, headers: dict[str, str] | None = None) -> FcmPushSender:
        self.requests: list[httpx.Request] = []

        def handle(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return httpx.Response(status, headers=headers or {}, json={})

        http = httpx.Client(base_url="https://fcm.test", transport=httpx.MockTransport(handle))
        return FcmPushSender(http, project_id="sportapp-test", access_token=lambda: "acceso")

    def test_posts_data_message_with_bearer(self) -> None:
        self.sender(200).send("token-sintetico", {"type": "EVENT_REMINDER_24H"})
        request = self.requests[0]
        self.assertEqual(request.url.path, "/v1/projects/sportapp-test/messages:send")
        self.assertEqual(request.headers["Authorization"], "Bearer acceso")
        body = json.loads(request.content)
        self.assertEqual(body["message"]["token"], "token-sintetico")
        self.assertNotIn("notification", body["message"])

    def test_priority_follows_the_notification_and_credential_failure_is_transient(
        self,
    ) -> None:
        self.sender(200).send("t", {"priority": "NORMAL"})
        self.assertEqual(
            json.loads(self.requests[0].content)["message"]["android"], {"priority": "NORMAL"}
        )

        def broken() -> str:
            raise RuntimeError("credencial inválida")

        http = httpx.Client(
            base_url="https://fcm.test",
            transport=httpx.MockTransport(lambda _: httpx.Response(200)),
        )
        with self.assertRaises(PushFailed) as failed:
            FcmPushSender(http, project_id="p", access_token=broken).send("t", {})
        self.assertEqual(failed.exception.reason, "credential_error")

    def test_classifies_responses(self) -> None:
        with self.assertRaises(PushFailed) as transient:
            self.sender(429, {"Retry-After": "7"}).send("t", {})
        self.assertEqual(transient.exception.retry_after, 7.0)
        with self.assertRaises(PushFailed):
            self.sender(503).send("t", {})
        with self.assertRaises(PushRejected) as rejected:
            self.sender(404).send("t", {})
        self.assertEqual(rejected.exception.reason, "http_404")


class UsersAthleteDirectoryTest(unittest.TestCase):
    def directory(self, response: httpx.Response | Exception) -> UsersAthleteDirectory:
        self.bodies: list[dict[str, Any]] = []

        def handle(request: httpx.Request) -> httpx.Response:
            self.bodies.append(json.loads(request.content))
            if isinstance(response, Exception):
                raise response
            return response

        return UsersAthleteDirectory(
            httpx.Client(base_url="http://users", transport=httpx.MockTransport(handle))
        )

    def test_reads_page(self) -> None:
        user_id = uuid4()
        page = self.directory(
            httpx.Response(200, json={"user_ids": [str(user_id)], "next_cursor": "c"})
        ).search(sports=["running"], city="Bogotá", country_code="CO", cursor=None, limit=1000)
        self.assertEqual(page.user_ids, (user_id,))
        self.assertEqual(page.next_cursor, "c")
        self.assertEqual(
            self.bodies[0],
            {
                "sports": ["running"],
                "city": "Bogotá",
                "country_code": "CO",
                "limit": 1000,
                "cursor": None,
            },
        )

    def test_any_failure_is_unavailable(self) -> None:
        for response in (
            httpx.Response(503, json={}),
            httpx.Response(422, json={}),
            httpx.Response(200, json={"user_ids": ["x"], "next_cursor": None}),
            httpx.ConnectError("caído"),
        ):
            with self.subTest(response=response), self.assertRaises(RecipientsUnavailable):
                self.directory(response).search(
                    sports=["running"], city="Bogotá", country_code="CO", cursor=None, limit=10
                )


class NotificationProjectionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = ReadStore()
        self.pushed: list[Notification] = []
        self.project = EventsProjector(
            unit_of_work=lambda: InMemoryProjectionUnitOfWork(self.store),
            clock=FixedClock(NOW),
            deliver_push=self.pushed.append,
        )

    def test_parses_and_projects_once_then_pushes_once(self) -> None:
        item = notification()
        message = parse_sport_event_message(requested_envelope(item))
        self.assertIsInstance(message, NotificationRequestedMessage)

        self.assertIs(self.project(message), ProjectionOutcome.PROJECTED)
        self.assertIs(self.project(message), ProjectionOutcome.DUPLICATE)
        other_delivery = parse_sport_event_message(requested_envelope(item))
        self.assertIs(self.project(other_delivery), ProjectionOutcome.STALE)

        self.assertEqual(self.store.inbox[item.notification_id], item)
        self.assertEqual(self.pushed, [item])

    def test_rebuilding_the_inbox_does_not_push_again(self) -> None:
        """I-2 (CMP-004): reconstruir la bandeja vuelve a proyectar, pero no reenvía el push."""
        item = notification()
        target = PushTarget(token_id=uuid4(), token="token-sintetico")
        deliveries = InMemoryDeliveries(targets_by_recipient={item.recipient_id: [target]})
        sender = ScriptedSender([])
        deliver = DeliverPush(
            store=deliveries, sender=sender, clock=FixedClock(NOW), sleep=lambda _: None
        )
        message = parse_sport_event_message(requested_envelope(item))
        for _ in range(2):
            rebuilt = ReadStore()  # vista y `processed_event` vacíos, como tras reconstruir
            projector = EventsProjector(
                unit_of_work=lambda store=rebuilt: InMemoryProjectionUnitOfWork(store),
                clock=FixedClock(NOW),
                deliver_push=deliver,
            )
            self.assertIs(projector(message), ProjectionOutcome.PROJECTED)
            self.assertEqual(rebuilt.inbox[item.notification_id], item)
        self.assertEqual([token for token, _ in sender.sent], ["token-sintetico"])
        self.assertEqual(len(deliveries.records), 1)

    def test_invalid_messages_are_rejected(self) -> None:
        item = notification()
        payload = requested_payload(item)
        for overrides in (
            {"aggregate_version": 2},
            {"schema_version": 2},
            {"aggregate_id": str(uuid4())},
            {"payload": {**payload, "type": "OTRO"}},
            {"payload": {**payload, "priority": "HIGH"}},
            {"payload": {**payload, "title": ""}},
            {"payload": {**payload, "created_at": "2026-10-12T16:00:00-05:00"}},
        ):
            with self.subTest(overrides=overrides), self.assertRaises(InvalidEvent):
                parse_sport_event_message(requested_envelope(item, **overrides))


class Message:
    def __init__(self, envelope: dict[str, Any]) -> None:
        self.data = json.dumps(envelope).encode()
        self.message_id = "m-1"
        self.delivery_attempt = 1
        self.acked = self.nacked = False

    def ack(self) -> None:
        self.acked = True

    def nack(self) -> None:
        self.nacked = True


class NotificationWorkerTest(unittest.TestCase):
    def envelope(self, event: SportEvent) -> dict[str, Any]:
        return {
            "event_id": str(uuid4()),
            "event_type": "evento.publicado.v1",
            "occurred_at": "2026-10-12T22:00:00Z",
            "producer": "events-management",
            "aggregate_type": "event",
            "aggregate_id": str(event.event_id),
            "aggregate_version": event.version,
            "correlation_id": str(uuid4()),
            "causation_id": str(uuid4()),
            "schema_version": 1,
            "payload": aggregate_document(event),
        }

    def test_acks_after_generation_and_nacks_on_failure(self) -> None:
        store = NotificationStore()
        athletes = [uuid4() for _ in range(3)]

        def generator(directory: FakeDirectory) -> GenerateEventMatchNotifications:
            return GenerateEventMatchNotifications(
                unit_of_work=lambda: InMemoryNotificationUnitOfWork(store),
                directory=directory,
                aliases=ALIASES,
                clock=FixedClock(NOW),
            )

        event = published_event()
        ok = Message(self.envelope(event))
        NotificationMessageHandler(generator(FakeDirectory(athletes)))(ok)
        self.assertTrue(ok.acked)
        self.assertEqual(len(store.notifications), 3)

        failing = Message(self.envelope(published_event()))
        NotificationMessageHandler(generator(FakeDirectory(athletes, fail_on_call=1)))(failing)
        self.assertTrue(failing.nacked)

    def test_other_types_are_acked_and_invalid_ones_nacked(self) -> None:
        handler = NotificationMessageHandler(lambda *_, **__: None)  # type: ignore[arg-type,return-value]
        other = Message({"event_type": "usuario.inscrito.v9"})
        handler(other)
        self.assertTrue(other.acked)
        invalid = Message({**self.envelope(published_event()), "aggregate_version": 1})
        handler(invalid)
        self.assertTrue(invalid.nacked)


class WorkerProcessTest(unittest.TestCase):
    def test_subscription_failure_is_an_error_exit(self) -> None:
        class Broken:
            def result(self) -> object:
                raise RuntimeError("NotFound")

        with self.assertLogs("sportapp.notifications", "ERROR"):
            self.assertEqual(await_subscription(Broken(), threading.Event()), 1)
        stopped = threading.Event()
        stopped.set()
        self.assertEqual(await_subscription(Broken(), stopped), 0)

    def test_background_push_does_not_block_and_drops_when_full(self) -> None:
        release = threading.Event()
        started: list[Notification] = []

        def slow(item: Notification) -> None:
            started.append(item)
            release.wait(5)

        background = BackgroundPush(slow, workers=1, queue_size=1)
        items = [notification() for _ in range(3)]
        with self.assertLogs("sportapp.projector", "WARNING") as logs:
            for item in items:
                background(item)  # vuelve de inmediato aunque el envío tarde
        self.assertIn("push.dropped", logs.output[0])
        release.set()
        background.close()
        self.assertEqual(started, items[:2])


class ReminderSchedulerSettingsTest(unittest.TestCase):
    def test_interval_never_exceeds_the_reminder_window(self) -> None:
        """M-2: con un periodo mayor que la ventana de 1800 s quedarían inicios sin evaluar."""

        def settings(seconds: float) -> ReminderSchedulerSettings:
            return ReminderSchedulerSettings(
                app_env="test",
                database_write_url="postgresql+psycopg://contract@127.0.0.1:1/contract",
                reminder_interval_seconds=seconds,
            )

        self.assertEqual(settings(900).reminder_interval_seconds, 900)
        window = (REMINDER_WINDOW_END - REMINDER_WINDOW_START).total_seconds()
        self.assertEqual(window, 1800)
        for invalid in (901, 1800, 3600, 59):
            with self.subTest(seconds=invalid), self.assertRaises(ValidationError):
                settings(invalid)


class StoreUnavailableTest(unittest.TestCase):
    def test_database_failures_become_store_unavailable(self) -> None:
        """M-5: el adaptador traduce la base inalcanzable para que la API responda `503`."""
        for failure in (
            OperationalError("SELECT 1", {}, Exception("conexión rechazada")),
            InterfaceError("SELECT 1", {}, Exception("conexión cerrada")),
            PoolTimeoutError("pool agotado"),
        ):

            def session_factory(failure: Exception = failure) -> Any:
                raise failure

            with self.subTest(failure=type(failure).__name__):
                with self.assertRaises(StoreUnavailable):
                    SqlAlchemyInboxReader(session_factory).page(uuid4(), None, 20)
                with self.assertRaises(StoreUnavailable):
                    SqlAlchemyPreferencesReader(session_factory).preferences(uuid4())

    def test_other_errors_are_not_masked(self) -> None:
        with self.assertRaises(ValueError), unavailable_on_failure():
            raise ValueError("defecto")


class OutboxOrderingKeyTest(unittest.TestCase):
    def test_notification_uses_its_own_ordering_key(self) -> None:
        base = {
            "event_id": uuid4(),
            "event_type": "notificacion.solicitada.v1",
            "schema_version": 1,
            "aggregate_type": "notification",
            "aggregate_id": uuid4(),
            "aggregate_version": 1,
            "occurred_at": NOW,
            "correlation_id": None,
            "causation_id": None,
            "payload": {},
            "attempts": 0,
        }
        recipient = str(uuid4())
        self.assertEqual(ordering_key(ClaimedEvent(**base, ordering_key=recipient)), recipient)
        self.assertEqual(ordering_key(ClaimedEvent(**base)), str(base["aggregate_id"]))


class NotificationsApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.inbox = InMemoryInbox()
        self.preferences = InMemoryPreferences()
        self.tokens = InMemoryPushTokens()
        self.athlete_id = uuid4()
        self.item = inbox_item(NOW)
        self.inbox.add(self.athlete_id, self.item)
        self.client = self.build(fcm_enabled=False)

    def build(self, *, fcm_enabled: bool) -> TestClient:
        clock = FixedClock(NOW + timedelta(hours=1))
        app = create_service_app(
            service_name="events-management",
            title="t",
            api_version="t",
            settings=ServiceSettings(app_env="test"),
            routers=[
                build_notifications_router(
                    verify_token=VERIFIER.verify,
                    list_notifications=ListMyNotifications(self.inbox),
                    mark_read=MarkNotificationRead(self.inbox, clock),
                    get_preferences=GetNotificationPreferences(self.preferences),
                    change_preferences=ChangeNotificationPreferences(
                        unit_of_work=lambda: self.preferences, reader=self.preferences, clock=clock
                    ),
                    register_push_token=RegisterPushToken(
                        store=self.tokens if fcm_enabled else None, clock=clock
                    ),
                )
            ],
        )
        return TestClient(app)

    def headers(self, actor: str = "athlete", user_id: UUID | None = None) -> dict[str, str]:
        return {"Authorization": f"Bearer {token(user_id or self.athlete_id, actor)}"}

    def test_requires_valid_token_and_athlete(self) -> None:
        for method, path in (
            ("GET", "/v1/me/notifications"),
            ("POST", f"/v1/me/notifications/{self.item.notification_id}/read"),
            ("GET", "/v1/me/notification-preferences"),
            ("PUT", "/v1/me/notification-preferences"),
            ("PUT", "/v1/me/push-tokens"),
        ):
            with self.subTest(path=path, method=method):
                self.assertEqual(self.client.request(method, path).status_code, 401)
                forbidden = self.client.request(method, path, headers=self.headers("organizer"))
                self.assertEqual(forbidden.status_code, 403)
                self.assertEqual(forbidden.json()["code"], "permission_required")

    def test_lists_and_marks_read(self) -> None:
        listed = self.client.get("/v1/me/notifications", headers=self.headers())
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.headers["Cache-Control"], "no-store")
        body = listed.json()
        self.assertEqual(body["next_cursor"], None)
        self.assertEqual(
            body["items"][0],
            {
                "notification_id": str(self.item.notification_id),
                "type": "EVENT_PUBLISHED_MATCH",
                "priority": "NORMAL",
                "event_id": str(self.item.event_id),
                "title": self.item.title,
                "body": self.item.body,
                "created_at": "2026-10-12T21:00:00Z",
                "read_at": None,
            },
        )
        read = self.client.post(
            f"/v1/me/notifications/{self.item.notification_id}/read", headers=self.headers()
        )
        self.assertEqual(read.status_code, 200)
        self.assertEqual(read.json()["read_at"], "2026-10-12T22:00:00Z")

    def test_query_and_path_errors(self) -> None:
        cases = {
            "/v1/me/notifications?limit=0": ("query.limit", "out_of_range"),
            "/v1/me/notifications?limit=abc": ("query.limit", "out_of_range"),
            "/v1/me/notifications?cursor=zzz": ("query.cursor", "invalid_cursor"),
            f"/v1/me/notifications?cursor={'a' * 513}": ("query.cursor", "invalid_cursor"),
        }
        unauthenticated = self.client.get("/v1/me/notifications?limit=abc")
        self.assertEqual(unauthenticated.status_code, 401)
        for path, (field, issue) in cases.items():
            with self.subTest(path=path):
                response = self.client.get(path, headers=self.headers())
                self.assertEqual(response.status_code, 422)
                self.assertEqual(response.json()["details"], [{"field": field, "issue": issue}])
        bad_id = self.client.post("/v1/me/notifications/abc/read", headers=self.headers())
        self.assertEqual(bad_id.status_code, 422)
        missing = self.client.post(f"/v1/me/notifications/{uuid4()}/read", headers=self.headers())
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.json()["code"], "notification_not_found")
        foreign = self.client.post(
            f"/v1/me/notifications/{self.item.notification_id}/read",
            headers=self.headers(user_id=uuid4()),
        )
        self.assertEqual(foreign.status_code, 404)

    def test_preferences_round_trip_and_conflict(self) -> None:
        initial = self.client.get("/v1/me/notification-preferences", headers=self.headers())
        self.assertEqual(
            initial.json(),
            {"event_matches": True, "event_reminders": True, "push_enabled": True, "version": 0},
        )
        body = {
            "event_matches": False,
            "event_reminders": True,
            "push_enabled": False,
            "expected_version": 0,
        }
        changed = self.client.put(
            "/v1/me/notification-preferences", json=body, headers=self.headers()
        )
        self.assertEqual(changed.status_code, 200)
        self.assertEqual(changed.json()["version"], 1)
        conflict = self.client.put(
            "/v1/me/notification-preferences", json=body, headers=self.headers()
        )
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.json()["code"], "version_conflict")
        self.assertEqual(conflict.json()["details"], [{"field": "expected_version", "issue": "1"}])

    def test_preferences_validation(self) -> None:
        cases: list[tuple[Any, list[dict[str, Any]]]] = [
            (None, [{"field": None, "issue": "required"}]),
            ([], [{"field": None, "issue": "unknown_value"}]),
            (
                {},
                [
                    {"field": "event_matches", "issue": "required"},
                    {"field": "event_reminders", "issue": "required"},
                    {"field": "push_enabled", "issue": "required"},
                    {"field": "expected_version", "issue": "required"},
                ],
            ),
            (
                {
                    "event_matches": "si",
                    "event_reminders": True,
                    "push_enabled": True,
                    "expected_version": -1,
                    "email": "x",
                },
                [
                    {"field": "email", "issue": "field_not_allowed"},
                    {"field": "event_matches", "issue": "unknown_value"},
                    {"field": "expected_version", "issue": "out_of_range"},
                ],
            ),
        ]
        for body, details in cases:
            with self.subTest(body=body):
                response = self.client.put(
                    "/v1/me/notification-preferences",
                    content=json.dumps(body),
                    headers={**self.headers(), "Content-Type": "application/json"},
                )
                self.assertEqual(response.status_code, 422)
                self.assertEqual(response.json()["details"], details)

    def test_unavailable_store_is_503(self) -> None:
        def down(*_: Any) -> Any:
            raise StoreUnavailable()

        self.inbox.page = down  # type: ignore[method-assign]
        self.inbox.mark_read = down  # type: ignore[method-assign]
        self.preferences.preferences = down  # type: ignore[method-assign]
        for method, path in (
            ("GET", "/v1/me/notifications"),
            ("POST", f"/v1/me/notifications/{self.item.notification_id}/read"),
            ("GET", "/v1/me/notification-preferences"),
        ):
            with self.subTest(path=path):
                response = self.client.request(method, path, headers=self.headers())
                self.assertEqual(response.status_code, 503)
                self.assertEqual(response.json()["code"], "upstream_unavailable")

    def test_push_token(self) -> None:
        body = {"token": "token-sintetico", "platform": "android"}
        disabled = self.client.put("/v1/me/push-tokens", json=body, headers=self.headers())
        self.assertEqual(disabled.status_code, 409)
        self.assertEqual(disabled.json()["code"], "push_not_enabled")

        enabled = self.build(fcm_enabled=True)
        response = enabled.put("/v1/me/push-tokens", json=body, headers=self.headers())
        self.assertEqual(response.status_code, 204)
        self.assertEqual(response.content, b"")
        self.assertEqual(self.tokens.tokens["token-sintetico"][0], self.athlete_id)
        invalid = enabled.put(
            "/v1/me/push-tokens",
            json={"token": "", "platform": "ios"},
            headers=self.headers(),
        )
        self.assertEqual(
            invalid.json()["details"],
            [
                {"field": "token", "issue": "invalid_length"},
                {"field": "platform", "issue": "unknown_value"},
            ],
        )


if __name__ == "__main__":
    unittest.main()
