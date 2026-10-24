"""BFF web, HU002: cuenta propia de cualquier actor (ADR-035).

Autenticación en el borde (sin filtro de actor ni capacidad), reenvío literal del cuerpo, la
clave, el token y la correlación, respuestas del servicio sin alterar, `no-store` y
minimización en logs. Usuarios (JWKS y cuenta) se simula con `httpx.MockTransport`; la app se
crea una vez por clase porque cada `httpx.Client()` es costoso en Windows.
"""

import json
import logging
import unittest
import uuid
from typing import Any, ClassVar

import httpx
from fastapi.testclient import TestClient
from test_profile_api import WEB_ORIGIN, _Capture, _Users, error
from test_training import token

from bff_web.main import API_VERSION, create_app
from bff_web.settings import BffWebSettings
from sportapp_platform.correlation import CORRELATION_HEADER

ACCOUNT = "/v1/me/account"
USER_ID = str(uuid.uuid4())
BODY = {"expected_version": 1, "full_name": "Ana María Pérez", "email": "Ana.Maria@Example.com"}
CONFIRMED = {
    "user_id": USER_ID,
    "full_name": "Ana María Pérez",
    "email": "ana.maria@example.com",
    "actor_type": "organizer",
    "status": "active",
    "version": 2,
    "updated_at": "2026-10-24T15:30:00Z",
}
PROJECTED = {
    **{k: v for k, v in CONFIRMED.items() if k != "updated_at"},
    "projected_at": "2026-10-24T15:30:01Z",
}
ACTORS = ("athlete", "organizer", "service_provider")


class AccountRoutesTest(unittest.TestCase):
    users: ClassVar[_Users]
    client: ClassVar[TestClient]

    @classmethod
    def setUpClass(cls) -> None:
        cls.users = _Users()
        cls.client = TestClient(
            create_app(
                BffWebSettings(
                    app_env="test",  # type: ignore[arg-type]
                    users_management_base_url="http://users.invalid",  # type: ignore[arg-type]
                    affiliation_management_base_url="http://affiliation.invalid",  # type: ignore[arg-type]
                    training_management_base_url="http://training.invalid",  # type: ignore[arg-type]
                    complementary_services_base_url="http://services.invalid",  # type: ignore[arg-type]
                    events_management_base_url="http://events.invalid",  # type: ignore[arg-type]
                    cors_allowed_origins=(WEB_ORIGIN,),
                ),
                users_http=httpx.Client(
                    transport=httpx.MockTransport(cls.users), base_url="http://users"
                ),
            )
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.client.close()

    def setUp(self) -> None:
        self.users.reset()
        self.users.response = httpx.Response(200, json=CONFIRMED)

    def put(self, body: Any = None, actor: str | None = "organizer", **headers: str) -> Any:
        sent = dict(headers)
        if actor is not None:
            sent["Authorization"] = f"Bearer {token(actor)}"
        if isinstance(body, bytes):
            return self.client.put(ACCOUNT, content=body, headers=sent)
        return self.client.put(ACCOUNT, json=BODY if body is None else body, headers=sent)

    def get(self, actor: str | None = "organizer") -> Any:
        sent = {"Authorization": f"Bearer {token(actor)}"} if actor is not None else {}
        return self.client.get(ACCOUNT, headers=sent)

    def test_contract_version(self) -> None:
        self.assertEqual(API_VERSION, "0.27.0")

    def test_every_actor_edits_and_reads_its_account(self) -> None:
        """CA1: los tres actores; sin `403` ni capacidad de plan."""
        for actor in ACTORS:
            with self.subTest(actor=actor):
                self.users.reset()
                self.users.response = httpx.Response(200, json={**CONFIRMED, "actor_type": actor})
                put = self.put(actor=actor, **{"Idempotency-Key": str(uuid.uuid4())})
                self.users.response = httpx.Response(200, json={**PROJECTED, "actor_type": actor})
                get = self.get(actor)

                self.assertEqual((put.status_code, get.status_code), (200, 200), put.text)
                self.assertEqual(put.json()["actor_type"], actor)
                self.assertEqual(get.json()["version"], 2)
                self.assertEqual(put.headers["cache-control"], "no-store")
                self.assertEqual(get.headers["cache-control"], "no-store")

    def test_forwards_body_key_token_and_correlation_verbatim(self) -> None:
        key = str(uuid.uuid4())
        correlation = str(uuid.uuid4())
        raw = b'{"expected_version": 1, "full_name": " Ana ", "email": "x@y.co", "user_id": "z"}'
        bearer = f"Bearer {token('athlete')}"

        self.client.put(
            ACCOUNT,
            content=raw,
            headers={
                "Authorization": bearer,
                "Idempotency-Key": key,
                CORRELATION_HEADER: correlation,
            },
        )

        [request] = self.users.requests
        self.assertEqual((request.method, request.url.path), ("PUT", ACCOUNT))
        self.assertEqual(request.content, raw)
        self.assertEqual(request.headers["Idempotency-Key"], key)
        self.assertEqual(request.headers["Authorization"], bearer)
        self.assertEqual(request.headers[CORRELATION_HEADER], correlation)

    def test_service_errors_pass_through_unchanged(self) -> None:
        """CA2, CA3, CA4 y CA5: `422`, `409` y el reintento llegan como los decide el servicio."""
        cases = [
            (422, error("validation_error", [{"field": "user_id", "issue": "field_not_allowed"}])),
            (409, error("email_already_registered")),
            (409, error("version_conflict", [{"field": "expected_version", "issue": "3"}])),
            (409, error("idempotency_key_reused")),
            (401, error("invalid_access_token")),
        ]
        for status, document in cases:
            with self.subTest(code=document["code"]):
                self.users.response = httpx.Response(status, json=document)

                response = self.put()

                self.assertEqual(response.status_code, status)
                self.assertEqual(response.json(), document)
                self.assertEqual(response.headers["cache-control"], "no-store")

    def test_replay_header_is_forwarded(self) -> None:
        self.users.response = httpx.Response(
            200, json=CONFIRMED, headers={"Idempotent-Replayed": "true"}
        )

        response = self.put(**{"Idempotency-Key": str(uuid.uuid4())})

        self.assertEqual(response.headers["idempotent-replayed"], "true")

    def test_read_statuses(self) -> None:
        for status, document in (
            (404, error("user_not_found")),
            (503, error("upstream_unavailable")),
        ):
            with self.subTest(status=status):
                self.users.response = httpx.Response(status, json=document)

                response = self.get()

                self.assertEqual((response.status_code, response.json()), (status, document))

    def test_token_is_required_before_calling_the_service(self) -> None:
        for response in (self.put(actor=None), self.get(actor=None)):
            self.assertEqual(response.status_code, 401)
            self.assertEqual(response.json()["code"], "invalid_access_token")
            self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(self.users.requests, [])

    def test_unexpected_statuses_and_transport_failures(self) -> None:
        for status in (500, 403, 204):
            with self.subTest(status=status):
                self.users.response = httpx.Response(status, json=error("x"))

                self.assertEqual(self.put().status_code, 503)
        for failure, status, code in (
            (httpx.ReadTimeout, 504, "upstream_timeout"),
            (httpx.ConnectError, 503, "upstream_unavailable"),
        ):
            with self.subTest(failure=failure.__name__):
                self.users.failure = failure

                for response in (self.put(), self.get()):
                    self.assertEqual(
                        (response.status_code, response.json()["code"]), (status, code)
                    )
        self.users.failure = None

    def test_logs_never_contain_name_or_email(self) -> None:
        capture = _Capture()
        root = logging.getLogger()
        root.addHandler(capture)
        self.addCleanup(root.removeHandler, capture)
        self.users.response = httpx.Response(500, json=error("x"))

        self.put()
        self.users.response = httpx.Response(200, json=PROJECTED)
        self.get()

        text = json.dumps(capture.text, ensure_ascii=False)
        for value in ("Ana María", "ana.maria@example.com", "Ana.Maria@Example.com"):
            self.assertNotIn(value, text)
