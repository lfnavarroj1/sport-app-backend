"""HU031 (ADR-042 §11-§12) sin infraestructura: contrato de `actividad.importada.v1`, proyección
en `training_session_history` con dobles en memoria, ACK/NACK del consumidor y registros sin el
nombre de la actividad."""

import json
import os
import threading
import unittest
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest import mock
from uuid import UUID

from fakes import NOW, FixedClock
from leak_checks import leaks

from sportapp_platform.config import ConfigurationError
from training_management.application.external_activity import (
    EVENT_TYPE,
    ExternalActivity,
    ExternalActivityImported,
    parse_external_activity_event,
)
from training_management.application.messages import InvalidEvent, UnsupportedEvent
from training_management.application.ports import DuplicateDelivery
from training_management.application.projections.external_activity_projection import (
    ProjectExternalActivity,
)
from training_management.application.projections.plan_projection import ProjectionOutcome
from training_management.domain.plans.model import Sport
from training_management.settings import (
    EXTERNAL_ACTIVITY_SUBSCRIPTION,
    ExternalActivityConsumerSettings,
)
from training_management.workers.external_activity_consumer import (
    ExternalActivityMessageHandler,
    await_subscription,
    ensure_resources,
    main,
)

LOGGER = "sportapp.external_activity_consumer"
# Nombre sintético reconocible: nunca debe aparecer en los registros.
ACTIVITY_NAME = "Fondo largo junto al río Medellín"
STARTED = datetime(2026, 10, 20, 11, 0, tzinfo=UTC)


def utc(instant: datetime) -> str:
    return instant.isoformat().replace("+00:00", "Z")


def activity_payload(activity_id: UUID | None = None, **overrides: Any) -> dict[str, Any]:
    activity_id = activity_id or uuid.uuid5(uuid.NAMESPACE_URL, f"strava:{uuid.uuid4().int}")
    values: dict[str, Any] = {
        "activity_id": str(activity_id),
        "user_id": str(uuid.uuid4()),
        "provider": "STRAVA",
        "external_activity_id": "12345678901",
        "sport": "running",
        "name": ACTIVITY_NAME,
        "started_at": utc(STARTED),
        "ended_at": utc(STARTED + timedelta(seconds=3725)),
        "time_zone": "America/Bogota",
        "duration_seconds": 3725,
        "moving_seconds": 3600,
        "distance_meters": 10234,
        "elevation_gain_meters": 85,
        "mapping_version": "strava-activity.v1",
        "version": 1,
    }
    values.update(overrides)
    return values


def activity_envelope(payload: dict[str, Any] | None = None, **overrides: Any) -> dict[str, Any]:
    payload = payload if payload is not None else activity_payload()
    values: dict[str, Any] = {
        "event_id": str(uuid.uuid4()),
        "event_type": EVENT_TYPE,
        "occurred_at": utc(NOW),
        "producer": "third-party-management",
        "aggregate_type": "external_activity",
        "aggregate_id": payload.get("activity_id"),
        "aggregate_version": payload.get("version"),
        "correlation_id": str(uuid.uuid4()),
        "causation_id": str(uuid.uuid4()),
        "schema_version": 1,
        "payload": payload,
    }
    values.update(overrides)
    return values


def without(document: dict[str, Any], key: str) -> dict[str, Any]:
    return {name: value for name, value in document.items() if name != key}


def with_payload(**overrides: Any) -> dict[str, Any]:
    return activity_envelope(activity_payload(**overrides))


class ParseExternalActivityTest(unittest.TestCase):
    def test_valid_event(self) -> None:
        payload = activity_payload()
        envelope = activity_envelope(payload)

        event = parse_external_activity_event(envelope)

        activity = event.activity
        self.assertEqual(event.event_id, UUID(envelope["event_id"]))
        self.assertEqual(event.correlation_id, UUID(envelope["correlation_id"]))
        self.assertEqual(event.causation_id, UUID(envelope["causation_id"]))
        self.assertEqual(activity.activity_id, UUID(payload["activity_id"]))
        self.assertEqual(activity.user_id, UUID(payload["user_id"]))
        self.assertEqual(activity.sport, Sport.RUNNING)
        self.assertEqual(activity.started_at, STARTED)
        self.assertEqual(activity.ended_at, STARTED + timedelta(seconds=3725))
        self.assertEqual(
            (activity.duration_seconds, activity.distance_meters, activity.version),
            (3725, 10234, 1),
        )
        # El nombre se valida, pero no se conserva en el tipo de aplicación.
        self.assertNotIn(ACTIVITY_NAME, repr(event))

    def test_nullable_fields_and_fractional_seconds(self) -> None:
        event = parse_external_activity_event(
            activity_envelope(
                activity_payload(
                    name=None,
                    moving_seconds=None,
                    elevation_gain_meters=None,
                    started_at="2026-10-20T11:00:00.5Z",
                    ended_at="2026-10-20T11:00:00.5Z",
                    duration_seconds=1,
                    distance_meters=0,
                ),
                correlation_id=None,
            )
        )

        self.assertIsNone(event.correlation_id)
        self.assertEqual(event.activity.distance_meters, 0)

    def test_other_event_types_are_unsupported(self) -> None:
        for event_type in ("actividad.importada.v2", "cuenta.vinculada.v1"):
            with self.subTest(event_type=event_type), self.assertRaises(UnsupportedEvent):
                parse_external_activity_event(activity_envelope(event_type=event_type))

    def test_contract_violations(self) -> None:
        envelope = activity_envelope()
        payload = envelope["payload"]
        cases: list[tuple[str, object]] = [
            ("envelope_not_object", []),
            ("event_type_missing", activity_envelope(event_type=None)),
            ("envelope_missing_fields", without(envelope, "causation_id")),
            ("envelope_unexpected_fields", {**envelope, "ordering_key": "x"}),
            ("unsupported_schema_version", activity_envelope(schema_version=2)),
            ("unsupported_schema_version", activity_envelope(schema_version=True)),
            ("unknown_producer", activity_envelope(producer="training-management")),
            ("unknown_aggregate_type", activity_envelope(aggregate_type="training_session")),
            ("occurred_at_not_utc", activity_envelope(occurred_at="2026-10-12T07:00:00-05:00")),
            ("payload_not_object", {**envelope, "payload": "x"}),
            ("payload_missing_fields", activity_envelope(without(payload, "name"))),
            ("payload_unexpected_fields", activity_envelope({**payload, "raw": {"id": 1}})),
            ("unsupported_mapping_version", with_payload(mapping_version="strava-activity.v2")),
            ("unknown_provider", with_payload(provider="GARMIN")),
            ("external_activity_id_invalid", with_payload(external_activity_id=12345)),
            ("external_activity_id_invalid", with_payload(external_activity_id="abc")),
            ("name_invalid", with_payload(name="")),
            ("name_invalid", with_payload(name="x" * 121)),
            ("name_invalid", with_payload(name=7)),
            ("time_zone_invalid", with_payload(time_zone="")),
            ("moving_seconds_out_of_range", with_payload(moving_seconds=-1)),
            ("elevation_gain_meters_out_of_range", with_payload(elevation_gain_meters=1.5)),
            ("unknown_sport", with_payload(sport="golf")),
            ("started_at_not_utc", with_payload(started_at="2026-10-20T11:00:00+00:00")),
            ("started_at_not_utc", with_payload(started_at=1760958000)),
            ("ended_at_not_utc", with_payload(ended_at="2026-02-30T12:00:00Z")),
            ("ended_before_started", with_payload(ended_at=utc(STARTED - timedelta(seconds=1)))),
            ("activity_id_not_uuid", activity_envelope({**payload, "activity_id": "x"})),
            ("user_id_not_uuid", with_payload(user_id=1)),
            ("duration_seconds_out_of_range", with_payload(duration_seconds=0)),
            ("duration_seconds_out_of_range", with_payload(duration_seconds=True)),
            ("duration_seconds_out_of_range", with_payload(duration_seconds=2**31)),
            ("distance_meters_out_of_range", with_payload(distance_meters=-1)),
            ("distance_meters_out_of_range", with_payload(distance_meters=10.5)),
            ("version_out_of_range", activity_envelope(activity_payload(version=0))),
            ("aggregate_id_mismatch", activity_envelope(aggregate_id=str(uuid.uuid4()))),
            ("aggregate_version_mismatch", activity_envelope(aggregate_version=2)),
            ("aggregate_version_out_of_range", activity_envelope(aggregate_version="1")),
            ("event_id_not_uuid", activity_envelope(event_id="x")),
            ("correlation_id_not_uuid", activity_envelope(correlation_id="x")),
            ("causation_id_not_uuid", activity_envelope(causation_id=None)),
        ]
        for reason, body in cases:
            with self.subTest(reason=reason), self.assertRaises(InvalidEvent) as raised:
                parse_external_activity_event(body)
            self.assertEqual(raised.exception.reason, reason)
            self.assertNotIn(ACTIVITY_NAME, str(raised.exception))


@dataclass
class HistoryRow:
    activity: ExternalActivity | None
    source: str
    version: int
    projected_at: datetime


@dataclass
class InMemoryExternalStore:
    rows: dict[UUID, HistoryRow] = field(default_factory=dict)
    processed: dict[UUID, tuple[str, UUID]] = field(default_factory=dict)
    duplicate_on_commit: bool = False
    commits: int = 0


class InMemoryExternalUnitOfWork:
    """Misma semántica que `SqlAlchemyExternalActivityUnitOfWork`: los cambios se aplican al
    confirmar; una fila de HU032 (`SPORTAPP`) nunca se reemplaza."""

    def __init__(self, store: InMemoryExternalStore) -> None:
        self.store = store
        self.staged_rows: dict[UUID, HistoryRow] = {}
        self.staged_processed: dict[UUID, tuple[str, UUID]] = {}

    def __enter__(self) -> "InMemoryExternalUnitOfWork":
        return self

    def __exit__(self, *_: object) -> None:
        self.staged_rows.clear()
        self.staged_processed.clear()

    def is_processed(self, event_id: UUID) -> bool:
        return event_id in self.store.processed

    def save_external_session(self, activity: ExternalActivity, projected_at: datetime) -> bool:
        current = self.store.rows.get(activity.activity_id)
        if current is not None and (
            current.source != "STRAVA" or current.version >= activity.version
        ):
            return False
        self.staged_rows[activity.activity_id] = HistoryRow(
            activity, "STRAVA", activity.version, projected_at
        )
        return True

    def add_processed(
        self, event_id: UUID, event_type: str, aggregate_id: UUID, processed_at: datetime
    ) -> None:
        self.staged_processed[event_id] = (event_type, aggregate_id)

    def commit(self) -> None:
        if self.store.duplicate_on_commit:
            raise DuplicateDelivery()
        self.store.rows.update(self.staged_rows)
        self.store.processed.update(self.staged_processed)
        self.store.commits += 1


class ProjectExternalActivityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = InMemoryExternalStore()
        self.project = ProjectExternalActivity(
            unit_of_work=lambda: InMemoryExternalUnitOfWork(self.store), clock=FixedClock()
        )
        self.activity_id = uuid.uuid4()

    def event(self, version: int = 1, **overrides: Any) -> ExternalActivityImported:
        payload = activity_payload(self.activity_id, version=version, **overrides)
        return parse_external_activity_event(activity_envelope(payload))

    def test_new_activity_is_inserted_as_strava(self) -> None:
        event = self.event()

        self.assertEqual(self.project(event), ProjectionOutcome.PROJECTED)

        row = self.store.rows[self.activity_id]
        self.assertEqual((row.source, row.version, row.projected_at), ("STRAVA", 1, NOW))
        self.assertEqual(self.store.processed[event.event_id], (EVENT_TYPE, self.activity_id))

    def test_same_event_id_is_a_duplicate_without_effects(self) -> None:
        event = self.event()
        self.project(event)

        self.assertEqual(self.project(event), ProjectionOutcome.DUPLICATE)
        self.assertEqual(self.store.commits, 1)

    def test_higher_version_replaces_the_row(self) -> None:
        self.project(self.event(version=1))

        outcome = self.project(self.event(version=3, distance_meters=12000))

        self.assertEqual(outcome, ProjectionOutcome.PROJECTED)
        row = self.store.rows[self.activity_id]
        self.assertEqual(row.version, 3)
        assert row.activity is not None
        self.assertEqual(row.activity.distance_meters, 12000)

    def test_lower_or_equal_version_is_ignored_but_recorded(self) -> None:
        self.project(self.event(version=2, distance_meters=9000))
        for version in (2, 1):
            with self.subTest(version=version):
                event = self.event(version=version, distance_meters=1)

                self.assertEqual(self.project(event), ProjectionOutcome.STALE)
                row = self.store.rows[self.activity_id]
                assert row.activity is not None
                self.assertEqual((row.version, row.activity.distance_meters), (2, 9000))
                self.assertIn(event.event_id, self.store.processed)

    def test_concurrent_delivery_that_commits_first_is_a_duplicate(self) -> None:
        self.store.duplicate_on_commit = True

        self.assertEqual(self.project(self.event()), ProjectionOutcome.DUPLICATE)
        self.assertEqual(self.store.rows, {})

    def test_never_overwrites_a_sportapp_session(self) -> None:
        self.store.rows[self.activity_id] = HistoryRow(None, "SPORTAPP", 1, NOW)

        self.assertEqual(self.project(self.event(version=5)), ProjectionOutcome.STALE)
        self.assertEqual(self.store.rows[self.activity_id].source, "SPORTAPP")


@dataclass
class Message:
    body: dict[str, Any] | bytes
    message_id: str = "m-1"
    delivery_attempt: int | None = 1
    acked: bool = False
    nacked: bool = False

    @property
    def data(self) -> bytes:
        return self.body if isinstance(self.body, bytes) else json.dumps(self.body).encode()

    def ack(self) -> None:
        self.acked = True

    def nack(self) -> None:
        self.nacked = True


class ExternalActivityMessageHandlerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = InMemoryExternalStore()
        self.project = ProjectExternalActivity(
            unit_of_work=lambda: InMemoryExternalUnitOfWork(self.store), clock=FixedClock()
        )
        self.handler = ExternalActivityMessageHandler(self.project)

    def handle(self, body: dict[str, Any] | bytes) -> tuple[Message, list[Any]]:
        message = Message(body)
        with self.assertLogs(LOGGER, level="INFO") as logs:
            self.handler(message)
        return message, logs.records

    def test_valid_event_is_acked_after_commit(self) -> None:
        envelope = activity_envelope()

        message, records = self.handle(envelope)

        self.assertTrue(message.acked)
        self.assertFalse(message.nacked)
        self.assertEqual(self.store.commits, 1)
        [record] = records
        self.assertEqual(record.getMessage(), "external_activity.processed")
        self.assertEqual(record.fields["event_id"], envelope["event_id"])
        self.assertEqual(record.fields["activity_id"], envelope["aggregate_id"])
        self.assertEqual(record.fields["outcome"], "projected")

    def test_duplicate_is_acked(self) -> None:
        envelope = activity_envelope()
        self.handle(envelope)

        message, records = self.handle(envelope)

        self.assertTrue(message.acked)
        self.assertEqual(records[0].fields["outcome"], "duplicate")

    def test_other_event_type_is_acked_without_effects(self) -> None:
        message, records = self.handle(activity_envelope(event_type="cuenta.vinculada.v1"))

        self.assertTrue(message.acked)
        self.assertEqual((self.store.rows, self.store.processed), ({}, {}))
        self.assertEqual(records[0].getMessage(), "external_activity.ignored_event")

    def test_invalid_event_and_json_are_nacked_for_the_dlq(self) -> None:
        for body, reason in (
            (with_payload(sport="golf"), "unknown_sport"),
            (b"{no-json", "invalid_json"),
            (b"\xff\xfe", "invalid_json"),
        ):
            with self.subTest(reason=reason):
                message, records = self.handle(body)

                self.assertTrue(message.nacked)
                self.assertFalse(message.acked)
                self.assertEqual(records[0].getMessage(), "external_activity.invalid_event")
                self.assertEqual(records[0].fields["reason"], reason)
        self.assertEqual(self.store.rows, {})

    def test_processing_failure_is_nacked(self) -> None:
        def failing(_: ExternalActivityImported) -> ProjectionOutcome:
            raise RuntimeError("base de lectura caída")

        message = Message(activity_envelope())
        with self.assertLogs(LOGGER, level="WARNING") as logs:
            ExternalActivityMessageHandler(failing)(message)

        self.assertTrue(message.nacked)
        self.assertFalse(message.acked)
        self.assertEqual(logs.records[0].fields["error_type"], "RuntimeError")

    def test_activity_name_never_reaches_the_logs(self) -> None:
        bodies: list[dict[str, Any] | bytes] = [
            activity_envelope(),
            with_payload(sport="golf"),
            with_payload(name=ACTIVITY_NAME * 4),  # inválido por longitud
        ]
        for body in bodies:
            _, records = self.handle(body)
            for record in records:
                self.assertNotIn(ACTIVITY_NAME, record.getMessage())
                self.assertEqual(leaks(record.fields, [ACTIVITY_NAME, "Medellín"]), [])


class _Streaming:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error

    def result(self) -> object:
        if self.error is not None:
            raise self.error
        return None


class ConsumerProcessTest(unittest.TestCase):
    def test_signal_stop_exits_with_zero(self) -> None:
        stop = threading.Event()
        stop.set()

        with self.assertLogs(LOGGER, level="INFO") as logs:
            code = await_subscription(_Streaming(RuntimeError("cancelada")), stop)

        self.assertEqual(code, 0)
        self.assertEqual([r.getMessage() for r in logs.records], ["external_activity.stopping"])

    def test_subscription_failure_is_logged_as_error_and_exits_with_one(self) -> None:
        class NotFound(Exception):
            pass

        for streaming, error_type in (
            (_Streaming(NotFound("sin suscripción")), "NotFound"),
            (_Streaming(), None),
        ):
            with self.subTest(error_type=error_type):
                with self.assertLogs(LOGGER, level="ERROR") as logs:
                    code = await_subscription(streaming, threading.Event())

                self.assertEqual(code, 1)
                [record] = logs.records
                self.assertEqual(record.getMessage(), "external_activity.subscription_failed")
                self.assertEqual(record.fields["error_type"], error_type)  # type: ignore[attr-defined]

    def test_setup_refuses_to_run_outside_the_emulator(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True), self.assertRaises(ConfigurationError):
            ensure_resources("sportapp-local")
        with (
            mock.patch.dict(os.environ, {"PUBSUB_PROJECT_ID": "sportapp-local"}, clear=True),
            self.assertRaises(ConfigurationError),
        ):
            main(["setup"])

    def test_unknown_arguments_are_rejected(self) -> None:
        with self.assertRaises(ConfigurationError):
            main(["otra-cosa"])

    def test_settings_default_subscription(self) -> None:
        settings = ExternalActivityConsumerSettings(
            app_env="test",
            database_read_url="postgresql+psycopg://contract@127.0.0.1:1/contract",  # type: ignore[arg-type]
            pubsub_project_id="p",
        )

        self.assertEqual(settings.external_activity_subscription, EXTERNAL_ACTIVITY_SUBSCRIPTION)
        self.assertEqual(EXTERNAL_ACTIVITY_SUBSCRIPTION, "entrenamientos-actividades-externas-v1")


if __name__ == "__main__":
    unittest.main()
