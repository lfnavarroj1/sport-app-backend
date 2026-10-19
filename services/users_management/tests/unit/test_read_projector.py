"""Manejador de mensajes de `usuarios-read-projector` y preparación del emulador."""

import json
import logging
import os
import threading
import unittest
from dataclasses import dataclass
from typing import Any
from unittest import mock

from projection_fakes import (
    SYNTHETIC_CITY,
    SYNTHETIC_TEMPERATURE,
    SYNTHETIC_WEIGHT,
    FixedClock,
    InMemoryProjectionUnitOfWork,
    InMemoryReadStore,
    profile_envelope,
    registered_envelope,
)

from sportapp_platform.config import ConfigurationError
from users_management.application.messages import UsersEvent
from users_management.application.projections.users_projection import (
    ProjectionOutcome,
    UsersProjector,
)
from users_management.domain.profile.events import ProfileSection
from users_management.workers.pubsub_setup import ensure_resources, ensure_topic
from users_management.workers.read_projector import ProjectionMessageHandler, await_subscription


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


class ProjectionMessageHandlerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = InMemoryReadStore()
        self.handler = ProjectionMessageHandler(
            UsersProjector(
                unit_of_work=lambda: InMemoryProjectionUnitOfWork(self.store), clock=FixedClock()
            )
        )

    def handle(self, body: dict[str, Any] | bytes) -> tuple[Message, list[logging.LogRecord]]:
        message = Message(body)
        with self.assertLogs("sportapp.projector", level="INFO") as logs:
            self.handler(message)
        return message, logs.records

    def test_valid_event_is_acked_after_projection(self) -> None:
        body = profile_envelope(ProfileSection.PHYSIOLOGICAL)

        message, records = self.handle(body)

        self.assertTrue(message.acked)
        self.assertFalse(message.nacked)
        fields = records[-1].fields  # type: ignore[attr-defined]
        self.assertEqual(records[-1].getMessage(), "projector.processed")
        self.assertEqual(fields["outcome"], "applied")
        self.assertEqual(fields["event_id"], body["event_id"])
        self.assertEqual(fields["aggregate_id"], body["aggregate_id"])
        self.assertEqual(fields["aggregate_version"], 1)
        self.assertEqual(fields["correlation_id"], body["correlation_id"])
        self.assertEqual(fields["delivery_attempt"], 1)

    def test_logs_never_contain_profile_values(self) -> None:
        """CA4: ningún valor demográfico o fisiológico aparece en los logs del proyector."""
        bodies: list[dict[str, Any] | bytes] = [
            profile_envelope(ProfileSection.PHYSIOLOGICAL),
            profile_envelope(ProfileSection.DEMOGRAPHIC),
            # Inválido por un campo excluido: tampoco se registran los valores.
            profile_envelope(
                ProfileSection.DEMOGRAPHIC,
                payload={"city": SYNTHETIC_CITY, "weight_kg": SYNTHETIC_WEIGHT},
            ),
        ]
        for body in bodies:
            _, records = self.handle(body)
            rendered = " ".join(f"{r.getMessage()} {getattr(r, 'fields', {})}" for r in records)
            for value in (SYNTHETIC_WEIGHT, SYNTHETIC_TEMPERATURE, SYNTHETIC_CITY, "@example"):
                self.assertNotIn(value, rendered)

    def test_registration_log_omits_the_email(self) -> None:
        _, records = self.handle(registered_envelope(email="ana@example.com"))

        self.assertNotIn("ana@example.com", str(records[-1].fields))  # type: ignore[attr-defined]
        self.assertEqual(len(self.store.directory), 1)

    def test_invalid_event_is_not_acked(self) -> None:
        message, records = self.handle(
            profile_envelope(ProfileSection.DEMOGRAPHIC, schema_version=9)
        )

        self.assertTrue(message.nacked)
        self.assertFalse(message.acked)
        self.assertEqual(records[-1].fields["reason"], "unsupported_schema_version")  # type: ignore[attr-defined]
        self.assertEqual(self.store.commits, 0)

    def test_malformed_json_is_not_acked(self) -> None:
        message, records = self.handle(b"{no-json")

        self.assertTrue(message.nacked)
        self.assertEqual(records[-1].fields["reason"], "invalid_json")  # type: ignore[attr-defined]

    def test_unsupported_event_type_is_acked_without_effects(self) -> None:
        message, records = self.handle(
            profile_envelope(ProfileSection.DEMOGRAPHIC, event_type="usuario.desactivado.v1")
        )

        self.assertTrue(message.acked)
        self.assertEqual(records[-1].getMessage(), "projector.ignored_event")
        self.assertEqual(self.store.commits, 0)

    def test_projection_failure_is_not_acked(self) -> None:
        def failing(event: UsersEvent) -> ProjectionOutcome:
            raise RuntimeError("base de lectura caída")

        handler = ProjectionMessageHandler(failing)
        message = Message(profile_envelope(ProfileSection.DEMOGRAPHIC))
        with self.assertLogs("sportapp.projector", level="WARNING") as logs:
            handler(message)

        self.assertTrue(message.nacked)
        self.assertFalse(message.acked)
        self.assertEqual(logs.records[-1].fields["error_type"], "RuntimeError")  # type: ignore[attr-defined]

    def test_duplicate_delivery_is_acked_once_applied(self) -> None:
        body = profile_envelope(ProfileSection.DEMOGRAPHIC)
        self.handle(body)

        message, records = self.handle(body)

        self.assertTrue(message.acked)
        self.assertEqual(records[-1].fields["outcome"], "duplicate")  # type: ignore[attr-defined]


class PubSubSetupTest(unittest.TestCase):
    def test_refuses_to_run_outside_the_emulator(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ConfigurationError):
                ensure_resources("sportapp-test")
            with self.assertRaises(ConfigurationError):
                ensure_topic("sportapp-test", "sportapp.usuarios.v1")


class _Streaming:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error

    def result(self) -> object:
        if self.error is not None:
            raise self.error
        return None


class AwaitSubscriptionTest(unittest.TestCase):
    """HU010 (cierre E2E): un fin inesperado de la suscripción no se oculta como parada."""

    def test_signal_stop_exits_with_zero(self) -> None:
        stop = threading.Event()
        stop.set()

        with self.assertLogs("sportapp.projector", level="INFO") as logs:
            code = await_subscription(_Streaming(RuntimeError("cancelada")), stop)

        self.assertEqual(code, 0)
        self.assertEqual([r.getMessage() for r in logs.records], ["projector.stopping"])

    def test_subscription_failure_is_logged_as_error_and_exits_with_one(self) -> None:
        class NotFound(Exception):
            pass

        for streaming, error_type in (
            (_Streaming(NotFound("sin suscripción")), "NotFound"),
            (_Streaming(), None),
        ):
            with self.subTest(error_type=error_type):
                with self.assertLogs("sportapp.projector", level="ERROR") as logs:
                    code = await_subscription(streaming, threading.Event())

                self.assertEqual(code, 1)
                [record] = logs.records
                self.assertEqual(record.getMessage(), "projector.subscription_failed")
                self.assertEqual(record.fields["error_type"], error_type)  # type: ignore[attr-defined]


if __name__ == "__main__":
    unittest.main()
