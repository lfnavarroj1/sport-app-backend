"""HU021 con dobles en memoria: caso de uso y contrato HTTP de `POST /v1/events`."""

import time
import unittest
import uuid
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import jwt
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fakes import (
    FakePermissions,
    FixedClock,
    InMemoryEventCreationUnitOfWork,
    InMemoryStore,
    NobodyAllowed,
    event_body,
)
from fastapi.testclient import TestClient

from events_management.api.events import build_events_router
from events_management.api.payload import parse_event
from events_management.application.commands.create_event import (
    CreateEvent,
    CreateEventCommand,
)
from events_management.application.errors import IdempotencyKeyReused, PermissionRequired
from events_management.application.ports import Caller, PermissionPolicy
from events_management.domain.sport_events.created import event_document
from events_management.domain.validation import InvalidSportEvent
from sportapp_platform.app import create_service_app
from sportapp_platform.config import ServiceSettings
from sportapp_platform.tokens import AccessTokenVerifier, JwksUnavailable, public_jwk

ISSUER, AUDIENCE = "sportapp-users-management", "sportapp"
SIGNER = Ed25519PrivateKey.generate()
VERIFIER = AccessTokenVerifier(
    {"keys": [public_jwk(SIGNER.public_key())]}, issuer=ISSUER, audience=AUDIENCE
)


def token(user_id: UUID, actor_type: str | None, signer: Ed25519PrivateKey = SIGNER) -> str:
    now = int(time.time())
    claims: dict[str, Any] = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": str(user_id),
        "iat": now,
        "nbf": now,
        "exp": now + 900,
        "jti": str(uuid.uuid4()),
    }
    if actor_type is not None:
        claims["actor_type"] = actor_type
    return jwt.encode(
        claims, signer, algorithm="EdDSA", headers={"kid": public_jwk(signer.public_key())["kid"]}
    )


class CreateEventUseCaseTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = InMemoryStore()
        self.clock = FixedClock()
        self.organizer = Caller(uuid4(), "organizer")
        self.create = self.use_case()

    def use_case(self, permissions: PermissionPolicy | None = None) -> CreateEvent:
        return CreateEvent(
            unit_of_work=lambda: InMemoryEventCreationUnitOfWork(self.store),
            permissions=permissions or FakePermissions(),
            clock=self.clock,
        )

    def command(self, key: UUID | None = None, **overrides: Any) -> CreateEventCommand:
        return CreateEventCommand(
            event=parse_event(event_body(**overrides)),
            idempotency_key=key or uuid4(),
            correlation_id=uuid4(),
        )

    def test_creates_draft_with_outbox_in_one_commit(self) -> None:
        """CA1 y CA5: DRAFT v1 del organizador y `evento.creado.v1` con el estado completo."""
        command = self.command()

        result = self.create(self.organizer, command)

        event = result.event
        self.assertFalse(result.replayed)
        self.assertEqual(event.organizer_id, self.organizer.user_id)
        self.assertEqual((event.status.value, event.version), ("DRAFT", 1))
        self.assertEqual(self.store.events, {event.event_id: event})
        [message] = self.store.outbox
        self.assertEqual(message.event_type, "evento.creado.v1")
        self.assertEqual((message.aggregate_id, message.aggregate_version), (event.event_id, 1))
        self.assertEqual(message.causation_id, str(command.idempotency_key))
        self.assertEqual(message.correlation_id, command.correlation_id)
        self.assertEqual(message.payload, event_document(event))
        self.assertNotEqual(message.event_id, event.event_id)

    def test_actor_and_permission_are_required(self) -> None:
        """CA2: sin datos persistidos."""
        for caller, permissions in (
            (Caller(uuid4(), "athlete"), FakePermissions()),
            (Caller(uuid4(), "service_provider"), FakePermissions()),
            (self.organizer, NobodyAllowed()),
        ):
            with self.subTest(actor=caller.actor_type), self.assertRaises(PermissionRequired):
                self.use_case(permissions)(caller, self.command())

        self.assertEqual((self.store.events, self.store.outbox), ({}, []))

    def test_invalid_event_leaves_no_partial_data(self) -> None:
        """CA3."""
        with self.assertRaises(InvalidSportEvent):
            self.create(self.organizer, self.command(capacity=0))

        self.assertEqual(
            (self.store.events, self.store.outbox, self.store.idempotency), ({}, [], {})
        )

    def test_idempotent_retry_and_key_reuse(self) -> None:
        """CA4: mismo payload normalizado → mismo evento; otro payload → reutilizada."""
        key = uuid4()
        first = self.create(self.organizer, self.command(key))
        retry = self.create(self.organizer, self.command(key, name="  Carrera 10K Bogotá "))

        self.assertTrue(retry.replayed)
        self.assertEqual(retry.event, first.event)
        self.assertEqual((len(self.store.events), len(self.store.outbox)), (1, 1))
        with self.assertRaises(IdempotencyKeyReused):
            self.create(self.organizer, self.command(key, capacity=4000))

    def test_same_instant_with_other_notation_is_the_same_payload(self) -> None:
        key = uuid4()
        self.create(self.organizer, self.command(key))

        retry = self.create(self.organizer, self.command(key, starts_at="2026-11-15T07:00-05:00"))

        self.assertTrue(retry.replayed)

    def test_same_key_from_another_organizer_is_independent(self) -> None:
        key = uuid4()
        mine = self.create(self.organizer, self.command(key))
        theirs = self.create(Caller(uuid4(), "organizer"), self.command(key))

        self.assertFalse(theirs.replayed)
        self.assertNotEqual(mine.event.event_id, theirs.event.event_id)
        self.assertEqual(len(self.store.outbox), 2)

    def test_expired_key_creates_a_new_draft(self) -> None:
        key = uuid4()
        self.create(self.organizer, self.command(key))
        self.clock.advance(timedelta(hours=24, seconds=1))

        again = self.create(self.organizer, self.command(key, capacity=10))

        self.assertFalse(again.replayed)
        self.assertEqual(len(self.store.events), 2)

    def test_concurrent_retry_returns_the_winner(self) -> None:
        """CA4: dos solicitudes con la misma clave producen un evento y un mensaje."""
        key = uuid4()
        winner = self.use_case()
        units = iter(
            [
                InMemoryEventCreationUnitOfWork(self.store),
                InMemoryEventCreationUnitOfWork(
                    self.store, lambda: winner(self.organizer, self.command(key))
                ),
                InMemoryEventCreationUnitOfWork(self.store),
            ]
        )
        loser = CreateEvent(
            unit_of_work=units.__next__, permissions=FakePermissions(), clock=self.clock
        )

        result = loser(self.organizer, self.command(key))

        self.assertTrue(result.replayed)
        self.assertEqual((len(self.store.events), len(self.store.outbox)), (1, 1))

    def test_audit_without_free_text_or_location(self) -> None:
        with self.assertLogs("sportapp.audit", level="INFO") as logs:
            result = self.create(self.organizer, self.command())
            with self.assertRaises(PermissionRequired):
                self.create(Caller(uuid4(), "athlete"), self.command())

        created, rejected = (record.fields for record in logs.records)  # type: ignore[attr-defined]
        self.assertEqual(logs.records[0].getMessage(), "audit.event_created")
        self.assertEqual(created["result"], "created")
        self.assertEqual(created["event_id"], str(result.event.event_id))
        self.assertEqual(created["organizer_id"], str(self.organizer.user_id))
        self.assertIsNotNone(created["correlation_id"])
        self.assertEqual(
            (rejected["result"], rejected["reason"]), ("rejected", "permission_required")
        )
        for text in ("Carrera", "Bolívar", "Avenida", "Bogotá"):
            self.assertNotIn(text, str(created))


class EventsApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = InMemoryStore()
        self.organizer_id = uuid4()
        self.jwks_down = False

        def verify(raw: str) -> Any:
            if self.jwks_down:
                raise JwksUnavailable()
            return VERIFIER.verify(raw)

        app = create_service_app(
            service_name="events-management",
            title="t",
            api_version="t",
            settings=ServiceSettings(app_env="test"),
            routers=[
                build_events_router(
                    verify_token=verify,
                    create_event=CreateEvent(
                        unit_of_work=lambda: InMemoryEventCreationUnitOfWork(self.store),
                        permissions=FakePermissions(),
                        clock=FixedClock(),
                    ),
                )
            ],
        )
        self.client = TestClient(app)
        self.auth = {"Authorization": f"Bearer {token(self.organizer_id, 'organizer')}"}

    def post(self, body: Any, key: str | None = None, **headers: str) -> Any:
        sent = {**self.auth, **headers}
        if key is not None:
            sent["Idempotency-Key"] = key
        return self.client.post("/v1/events", json=body, headers=sent)

    def test_201_matches_the_adr_example(self) -> None:
        """CA1: respuesta de ADR-028 con instantes en UTC (`Z`) y la zona declarada."""
        key = str(uuid4())
        response = self.post(event_body(), key)
        retry = self.post(event_body(), key)

        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        self.assertEqual(response.headers["Location"], f"/v1/events/{body['event_id']}")
        self.assertEqual(body["organizer_id"], str(self.organizer_id))
        self.assertEqual(
            {
                name: body[name]
                for name in ("starts_at", "ends_at", "time_zone", "status", "version")
            },
            {
                "starts_at": "2026-11-15T12:00:00Z",
                "ends_at": "2026-11-15T15:00:00Z",
                "time_zone": "America/Bogota",
                "status": "DRAFT",
                "version": 1,
            },
        )
        self.assertEqual(body["location"], event_body()["location"])
        self.assertEqual((body["created_at"], body["updated_at"]), ("2026-10-12T21:00:00Z",) * 2)
        self.assertEqual(
            set(body),
            {
                "event_id",
                "organizer_id",
                "name",
                "description",
                "discipline",
                "location",
                "starts_at",
                "ends_at",
                "time_zone",
                "duration_minutes",
                "capacity",
                "status",
                "version",
                "created_at",
                "updated_at",
            },
        )
        self.assertNotIn("Idempotent-Replayed", response.headers)
        self.assertEqual(retry.json(), body)
        self.assertEqual(retry.headers["Idempotent-Replayed"], "true")

    def test_validation_error_contract(self) -> None:
        """CA3: `422 validation_error` con `details[{field, issue}]` y sin datos."""
        body = event_body(
            organizer_id=str(uuid4()),
            time_zone="Europe/Madrid",
            location={**event_body()["location"], "country_code": "ZZ"},
        )

        response = self.post(body, str(uuid4()))

        self.assertEqual(response.status_code, 422)
        payload = response.json()
        self.assertEqual(payload["code"], "validation_error")
        self.assertCountEqual(
            payload["details"],
            [
                {"field": "organizer_id", "issue": "field_not_allowed"},
                {"field": "time_zone", "issue": "time_zone_offset_mismatch"},
                {"field": "location.country_code", "issue": "unknown_value"},
            ],
        )
        self.assertEqual((self.store.events, self.store.outbox), ({}, []))

    def test_idempotency_key_header(self) -> None:
        missing = self.post(event_body(name="x"))
        invalid = self.post(event_body(), "no-es-uuid")

        self.assertEqual(missing.status_code, 422)
        self.assertCountEqual(
            missing.json()["details"],
            [
                {"field": "Idempotency-Key", "issue": "required"},
                {"field": "name", "issue": "invalid_length"},
            ],
        )
        self.assertEqual(
            invalid.json()["details"], [{"field": "Idempotency-Key", "issue": "unknown_value"}]
        )
        self.assertEqual(self.store.events, {})

    def test_key_reused_with_other_payload_is_409(self) -> None:
        """CA4."""
        key = str(uuid4())
        self.post(event_body(), key)

        response = self.post(event_body(capacity=10), key)

        self.assertEqual(
            (response.status_code, response.json()["code"]), (409, "idempotency_key_reused")
        )
        self.assertEqual(len(self.store.outbox), 1)

    def test_authentication_and_permission(self) -> None:
        """CA2: 401 o 403 sin persistir evento ni outbox."""
        anonymous = self.client.post("/v1/events", json=event_body())
        forged = self.post(
            event_body(),
            str(uuid4()),
            Authorization=f"Bearer {token(uuid4(), 'organizer', Ed25519PrivateKey.generate())}",
        )
        athlete = self.post(
            event_body(), str(uuid4()), Authorization=f"Bearer {token(uuid4(), 'athlete')}"
        )
        no_actor = self.post(
            event_body(), str(uuid4()), Authorization=f"Bearer {token(uuid4(), None)}"
        )
        self.jwks_down = True
        with self.assertLogs("sportapp.events", level="WARNING"):
            down = self.post(event_body(), str(uuid4()))

        self.assertEqual(anonymous.status_code, 401)
        self.assertEqual(anonymous.headers["WWW-Authenticate"], "Bearer")
        self.assertEqual((forged.status_code, forged.json()["code"]), (401, "invalid_access_token"))
        self.assertEqual(
            (athlete.status_code, athlete.json()["code"]), (403, "permission_required")
        )
        self.assertEqual(no_actor.status_code, 403)
        self.assertEqual((down.status_code, down.json()["code"]), (503, "upstream_unavailable"))
        self.assertEqual((self.store.events, self.store.outbox), ({}, []))

    def test_permission_is_checked_before_a_missing_key(self) -> None:
        response = self.post(
            event_body(), None, Authorization=f"Bearer {token(uuid4(), 'service_provider')}"
        )

        self.assertEqual(response.status_code, 403)


if __name__ == "__main__":
    unittest.main()
