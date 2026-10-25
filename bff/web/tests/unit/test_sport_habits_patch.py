"""BFF web, HU008: edición parcial de los hábitos deportivos (ADR-035).

`PATCH /v1/me/sport-habits`: filtro de borde por actor, reenvío literal del cuerpo, la clave,
el token y la correlación, `404`/`409`/`422` del servicio sin alterar, `no-store` y CORS con
`PATCH`. Las reglas de fusión y validación son de `users_management`. Usuarios se simula con
`httpx.MockTransport` (mismo doble que HU005).
"""

import json
import unittest
import uuid
from typing import Any, ClassVar

import httpx
from fastapi.testclient import TestClient
from test_profile_api import (
    SPORT_HABITS,
    SPORT_HABITS_SECTION,
    USER_ID,
    WEB_ORIGIN,
    _Users,
    error,
)
from test_training import token

from bff_web.main import create_app
from bff_web.settings import BffWebSettings
from sportapp_platform.correlation import CORRELATION_HEADER

PATCH_BODY = {"expected_version": 1, "primary_goal": None}
PATCHED = {"user_id": USER_ID, **SPORT_HABITS_SECTION, "version": 2, "primary_goal": None}


class SportHabitsPatchRoutesTest(unittest.TestCase):
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
        self.users.response = httpx.Response(200, json=PATCHED)

    def patch(self, body: Any = PATCH_BODY, actor: str | None = "athlete", **headers: str) -> Any:
        sent = dict(headers)
        if actor is not None:
            sent["Authorization"] = f"Bearer {token(actor)}"
        return self.client.patch(SPORT_HABITS, json=body, headers=sent)

    def test_without_token_or_with_another_actor_the_service_is_not_called(self) -> None:
        """Seguridad: sin sesión `401`; otro actor `403 permission_required`."""
        anonymous = self.patch(actor=None)
        self.assertEqual(
            (anonymous.status_code, anonymous.json()["code"]), (401, "invalid_access_token")
        )
        for actor in ("organizer", "service_provider"):
            with self.subTest(actor=actor):
                response = self.patch(actor=actor)
                self.assertEqual(
                    (response.status_code, response.json()["code"]),
                    (403, "permission_required"),
                )
                self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(self.users.requests, [])

    def test_body_key_token_and_correlation_are_forwarded_verbatim(self) -> None:
        """CA1-CA3: el BFF no fusiona ni valida; `user_id` llega al servicio, que responde `422`."""
        key, correlation_id = str(uuid.uuid4()), str(uuid.uuid4())
        access_token = token("athlete")
        body = {"expected_version": 1, "sports": [], "user_id": "inyectado"}

        self.client.patch(
            SPORT_HABITS,
            json=body,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Idempotency-Key": key,
                CORRELATION_HEADER: correlation_id,
            },
        )

        [forwarded] = self.users.requests
        self.assertEqual((forwarded.method, forwarded.url.path), ("PATCH", SPORT_HABITS))
        self.assertEqual(json.loads(forwarded.content), body)
        self.assertEqual(forwarded.headers["Idempotency-Key"], key)
        self.assertEqual(forwarded.headers["Authorization"], f"Bearer {access_token}")
        self.assertEqual(forwarded.headers[CORRELATION_HEADER], correlation_id)

    def test_malformed_and_empty_bodies_are_forwarded_as_bytes(self) -> None:
        raw = b'{"primary_goal": '
        malformed = self.client.patch(
            SPORT_HABITS,
            content=raw,
            headers={
                "Authorization": f"Bearer {token('athlete')}",
                "Content-Type": "application/json",
            },
        )
        empty = self.client.patch(
            SPORT_HABITS, headers={"Authorization": f"Bearer {token('athlete')}"}
        )

        self.assertEqual((malformed.status_code, empty.status_code), (200, 200))
        self.assertEqual([request.content for request in self.users.requests], [raw, b""])

    def test_success_and_replay_pass_through_with_no_store(self) -> None:
        """CA3: la respuesta del servicio trae el estado completo y la versión nueva."""
        for replayed in (None, "true"):
            with self.subTest(replayed=replayed):
                headers = {"Cache-Control": "no-store"}
                if replayed:
                    headers["Idempotent-Replayed"] = replayed
                self.users.response = httpx.Response(200, json=PATCHED, headers=headers)

                response = self.patch(**{"Idempotency-Key": str(uuid.uuid4())})

                self.assertEqual((response.status_code, response.json()), (200, PATCHED))
                self.assertEqual(response.headers.get("Idempotent-Replayed"), replayed)
                self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_service_errors_pass_through_unchanged(self) -> None:
        """CA5 y concurrencia: `404`, `409` y `422` del servicio sin alterar."""
        for status, payload in (
            (401, error("invalid_access_token")),
            (403, error("permission_required")),
            (404, error("sport_habits_not_found")),
            (409, error("version_conflict", [{"field": "expected_version", "issue": "3"}])),
            (409, error("idempotency_key_reused")),
            (422, error("validation_error", [{"field": None, "issue": "required"}])),  # type: ignore[dict-item]
            (422, error("validation_error", [{"field": "user_id", "issue": "field_not_allowed"}])),
        ):
            with self.subTest(status=status, code=payload["code"]):
                self.users.response = httpx.Response(status, json=payload)

                response = self.patch()

                self.assertEqual((response.status_code, response.json()), (status, payload))
                self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_unexpected_statuses_become_503(self) -> None:
        """El `PATCH` nunca crea: un `201` del servicio es tan inesperado como un `500`."""
        for status in (201, 204, 500):
            with self.subTest(status=status):
                self.users.response = httpx.Response(status, json=error("x"))
                with self.assertLogs("sportapp.bff", level="WARNING"):
                    response = self.patch()

                self.assertEqual(
                    (response.status_code, response.json()["code"]), (503, "upstream_unavailable")
                )

    def test_timeouts_and_transport_errors(self) -> None:
        for failure, status, code in (
            (httpx.ReadTimeout, 504, "upstream_timeout"),
            (httpx.ConnectError, 503, "upstream_unavailable"),
        ):
            with self.subTest(failure=failure.__name__):
                self.users.failure = failure

                response = self.patch()

                self.assertEqual((response.status_code, response.json()["code"]), (status, code))
                self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_cors_preflight_allows_patch_with_idempotency_key(self) -> None:
        response = self.client.options(
            SPORT_HABITS,
            headers={
                "Origin": WEB_ORIGIN,
                "Access-Control-Request-Method": "PATCH",
                "Access-Control-Request-Headers": "authorization, content-type, idempotency-key",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("PATCH", response.headers["access-control-allow-methods"])
        self.assertIn("idempotency-key", response.headers["access-control-allow-headers"].lower())


if __name__ == "__main__":
    unittest.main()
