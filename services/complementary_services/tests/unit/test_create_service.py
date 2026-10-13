"""HU015 con dobles en memoria: forma del JSON, caso de uso y contrato HTTP."""

import time
import unittest
import uuid
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import jwt
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fakes import (
    NOW,
    FakePermissions,
    FixedClock,
    InMemoryCreationUnitOfWork,
    InMemoryStore,
    interval,
    service_body,
)
from fastapi.testclient import TestClient

from complementary_services.api.payload import parse_offer
from complementary_services.api.services import build_services_router
from complementary_services.application.commands.create_service import (
    CreateService,
    CreateServiceCommand,
)
from complementary_services.application.errors import IdempotencyKeyReused, PermissionRequired
from complementary_services.application.ports import Caller
from complementary_services.domain.catalog.offer import ServiceStatus
from complementary_services.domain.validation import InvalidServiceOffer
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


class PayloadShapeTest(unittest.TestCase):
    def issues(self, body: object) -> set[tuple[str | None, str]]:
        return {(v.field, v.issue.value) for v in parse_offer(body).transport_violations}

    def test_provider_id_and_unknown_fields_are_not_allowed(self) -> None:
        """CA3: el cliente no envía `provider_id`."""
        body = service_body(provider_id=str(uuid4()), status="PUBLISHED")
        body["price"] = {**body["price"], "tax": "19"}
        body["availability"] = [{**interval(), "slot_id": "x"}]

        self.assertEqual(
            self.issues(body),
            {
                ("provider_id", "field_not_allowed"),
                ("status", "field_not_allowed"),
                ("price.tax", "field_not_allowed"),
                ("availability[0].slot_id", "field_not_allowed"),
            },
        )

    def test_required_and_json_types(self) -> None:
        body = service_body(name=None, price={"amount": 120000, "currency": "COP"})
        del body["description"]
        body["availability"] = ["no-es-objeto", {**interval(), "capacity": "1"}, interval()]

        self.assertEqual(
            self.issues(body),
            {
                ("name", "required"),
                ("description", "required"),
                ("price.amount", "unknown_value"),  # número JSON: la tarifa es cadena decimal
                ("price.billing_unit", "required"),
                ("availability[0]", "unknown_value"),
                ("availability[1].capacity", "unknown_value"),
            },
        )
        self.assertEqual(len(parse_offer(body).availability), 3, "los índices no se desplazan")

    def test_body_must_be_an_object(self) -> None:
        self.assertEqual(self.issues(None), {(None, "required")})
        self.assertEqual(self.issues([1]), {(None, "unknown_value")})
        self.assertEqual(
            self.issues(service_body(availability={"a": 1})), {("availability", "unknown_value")}
        )


class CreateServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = InMemoryStore()
        self.clock = FixedClock()
        self.provider = Caller(uuid4(), "service_provider")
        self.create = CreateService(
            unit_of_work=lambda: InMemoryCreationUnitOfWork(self.store),
            permissions=FakePermissions(),
            clock=self.clock,
        )

    def command(self, key: UUID | None = None, **body: Any) -> CreateServiceCommand:
        return CreateServiceCommand(
            offer=parse_offer(service_body(**body)), idempotency_key=key or uuid4()
        )

    def test_creates_draft_version_one_for_the_authenticated_provider(self) -> None:
        """CA1 y CA5."""
        result = self.create(self.provider, self.command())

        stored = self.store.services[result.service.offer.service_id]
        self.assertEqual((stored.status, stored.version), (ServiceStatus.DRAFT, 1))
        self.assertEqual(stored.provider_id, self.provider.user_id)
        self.assertEqual((stored.created_at, stored.updated_at), (NOW, NOW))
        self.assertFalse(result.replayed)

    def test_only_service_providers_create(self) -> None:
        """CA2."""
        for actor in ("athlete", "organizer"):
            with self.subTest(actor=actor), self.assertRaises(PermissionRequired):
                self.create(Caller(uuid4(), actor), self.command())
        self.assertEqual(self.store.services, {})

    def test_invalid_offer_leaves_nothing(self) -> None:
        """CA3."""
        with self.assertRaises(InvalidServiceOffer):
            self.create(
                self.provider,
                self.command(price={"amount": "-1", "currency": "COP", "billing_unit": "SESSION"}),
            )

        self.assertEqual((self.store.services, self.store.idempotency), ({}, {}))

    def test_idempotent_retry_and_key_reuse(self) -> None:
        """CA4."""
        key = uuid4()
        first = self.create(self.provider, self.command(key))
        retry = self.create(
            self.provider, self.command(key, name="  Valoración deportiva inicial ")
        )

        self.assertTrue(retry.replayed, "el payload normalizado es el mismo")
        self.assertEqual(retry.service, first.service)
        self.assertEqual(len(self.store.services), 1)
        with self.assertRaises(IdempotencyKeyReused):
            self.create(self.provider, self.command(key, name="Otro servicio distinto"))

    def test_same_key_from_another_provider_is_independent(self) -> None:
        key = uuid4()
        mine = self.create(self.provider, self.command(key))
        theirs = self.create(Caller(uuid4(), "service_provider"), self.command(key))

        self.assertFalse(theirs.replayed)
        self.assertNotEqual(mine.service.offer.service_id, theirs.service.offer.service_id)
        self.assertEqual(len(self.store.services), 2)

    def test_expired_key_creates_a_new_draft(self) -> None:
        key = uuid4()
        self.create(self.provider, self.command(key))
        self.clock.advance(timedelta(hours=25))

        again = self.create(
            self.provider,
            self.command(
                key,
                availability=[
                    interval(
                        starts_at="2026-10-22T09:00:00-05:00", ends_at="2026-10-22T10:00:00-05:00"
                    )
                ],
            ),
        )

        self.assertFalse(again.replayed)
        self.assertEqual(len(self.store.services), 2)

    def test_concurrent_retry_returns_the_winner(self) -> None:
        key = uuid4()
        winner = CreateService(
            unit_of_work=lambda: InMemoryCreationUnitOfWork(self.store),
            permissions=FakePermissions(),
            clock=self.clock,
        )
        units = iter(
            [
                InMemoryCreationUnitOfWork(self.store),
                InMemoryCreationUnitOfWork(
                    self.store, lambda: winner(self.provider, self.command(key))
                ),
                InMemoryCreationUnitOfWork(self.store),
            ]
        )
        loser = CreateService(
            unit_of_work=units.__next__, permissions=FakePermissions(), clock=self.clock
        )

        result = loser(self.provider, self.command(key))

        self.assertTrue(result.replayed)
        self.assertEqual(len(self.store.services), 1)

    def test_audit_without_offer_content(self) -> None:
        with self.assertLogs("sportapp.audit", level="INFO") as logs:
            self.create(self.provider, self.command())

        fields = logs.records[0].fields  # type: ignore[attr-defined]
        self.assertEqual(fields["result"], "created")
        self.assertNotIn("Valoración", str(fields))


class ServicesApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = InMemoryStore()
        self.provider_id = uuid4()
        self.jwks_down = False

        def verify(raw: str) -> Any:
            if self.jwks_down:
                raise JwksUnavailable()
            return VERIFIER.verify(raw)

        app = create_service_app(
            service_name="complementary-services",
            title="t",
            api_version="t",
            settings=ServiceSettings(app_env="test"),
            routers=[
                build_services_router(
                    verify_token=verify,
                    create_service=CreateService(
                        unit_of_work=lambda: InMemoryCreationUnitOfWork(self.store),
                        permissions=FakePermissions(),
                        clock=FixedClock(),
                    ),
                )
            ],
        )
        self.client = TestClient(app)
        self.auth = {"Authorization": f"Bearer {token(self.provider_id, 'service_provider')}"}

    def post(self, body: Any, key: str | None = None, **headers: str) -> Any:
        sent = {**self.auth, **headers}
        if key is not None:
            sent["Idempotency-Key"] = key
        return self.client.post("/v1/services", json=body, headers=sent)

    def test_201_contract_with_utc_instants(self) -> None:
        """CA1: respuesta de ADR-027, instantes en UTC con `Z` y zona declarada."""
        key = str(uuid4())
        response = self.post(service_body(), key)
        retry = self.post(service_body(), key)

        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        self.assertEqual(body["provider_id"], str(self.provider_id))
        self.assertEqual((body["status"], body["version"]), ("DRAFT", 1))
        self.assertEqual(
            body["price"], {"amount": "120000.00", "currency": "COP", "billing_unit": "SESSION"}
        )
        self.assertEqual(
            body["availability"],
            [
                {
                    "starts_at": "2026-10-20T14:00:00Z",
                    "ends_at": "2026-10-20T15:00:00Z",
                    "time_zone": "America/Bogota",
                    "capacity": 1,
                }
            ],
        )
        self.assertTrue(body["created_at"].endswith("Z"))
        self.assertEqual(response.headers["Location"], f"/v1/services/{body['service_id']}")
        self.assertNotIn("Idempotent-Replayed", response.headers)
        self.assertEqual(retry.json(), body)
        self.assertEqual(retry.headers["Idempotent-Replayed"], "true")
        self.assertNotIn("payment", response.text.lower())

    def test_adr_example_error(self) -> None:
        """CA3: el ejemplo de error de ADR-027."""
        body = service_body(
            provider_id=str(uuid4()), availability=[interval(time_zone="Europe/Madrid")]
        )

        response = self.post(body, str(uuid4()))

        self.assertEqual(response.status_code, 422)
        payload = response.json()
        self.assertEqual(payload["code"], "validation_error")
        self.assertCountEqual(
            payload["details"],
            [
                {"field": "availability[0].time_zone", "issue": "time_zone_offset_mismatch"},
                {"field": "provider_id", "issue": "field_not_allowed"},
            ],
        )
        self.assertEqual(self.store.services, {})

    def test_idempotency_key_header(self) -> None:
        missing = self.post(service_body(name="x"))
        invalid = self.post(service_body(), "no-es-uuid")

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

    def test_key_reused_with_other_payload_is_409(self) -> None:
        key = str(uuid4())
        self.post(service_body(), key)

        response = self.post(service_body(name="Otro servicio"), key)

        self.assertEqual(
            (response.status_code, response.json()["code"]), (409, "idempotency_key_reused")
        )

    def test_authentication_and_permission(self) -> None:
        """CA2."""
        anonymous = self.client.post("/v1/services", json=service_body())
        athlete = self.post(
            service_body(provider_id="x"),
            str(uuid4()),
            Authorization=f"Bearer {token(uuid4(), 'athlete')}",
        )
        no_actor = self.post(
            service_body(), str(uuid4()), Authorization=f"Bearer {token(uuid4(), None)}"
        )
        self.jwks_down = True
        with self.assertLogs("sportapp.services", level="WARNING"):
            down = self.post(service_body(), str(uuid4()))

        self.assertEqual(anonymous.status_code, 401)
        self.assertEqual(anonymous.headers["WWW-Authenticate"], "Bearer")
        self.assertEqual(
            (athlete.status_code, athlete.json()["code"]), (403, "permission_required")
        )
        self.assertEqual(no_actor.status_code, 403)
        self.assertEqual((down.status_code, down.json()["code"]), (503, "upstream_unavailable"))

    def test_permission_is_checked_before_a_missing_key(self) -> None:
        response = self.post(
            service_body(), None, Authorization=f"Bearer {token(uuid4(), 'organizer')}"
        )

        self.assertEqual(response.status_code, 403)


if __name__ == "__main__":
    unittest.main()
