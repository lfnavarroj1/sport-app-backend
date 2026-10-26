"""BFF móvil, HU038: reglas y preferencias de recordatorios durante la sesión (ADR-045).

El borde exige `athlete` y la capacidad vigente `nutrition.session.reminders`; lo demás llega
del servicio sin alterar: cuerpo (con `expected_version`), correlación, `409`, `422` y errores.
El router se monta solo, con los mismos clientes que `create_app`, para probar el borde.
"""

import json
import logging
import unittest
import uuid
from typing import Any

import httpx
from fastapi.testclient import TestClient
from test_capabilities import token
from test_devices import RecordedLogs, SlowableUpstream, error

from bff_mobile.api.session_reminders import build_session_reminders_router
from bff_mobile.clients.affiliation_management import AffiliationManagementClient
from bff_mobile.clients.training_management import TrainingManagementClient
from bff_mobile.clients.users_management import UsersManagementClient
from bff_mobile.settings import BffMobileSettings
from sportapp_platform.app import create_service_app
from sportapp_platform.tokens import CachedJwksVerifier

CAPABILITY = "nutrition.session.reminders"
RULES_PATH = "/v1/session-reminder-rules"
PREFERENCES_PATH = "/v1/me/session-reminder-preferences"
DISCLAIMER = "Recordatorio general: no es un diagnóstico médico ni prescribe cantidades."
RULES = {
    "ruleset_version": "session-reminders-2026-10-13.v1",
    "rules": [
        {
            "type": "HYDRATION",
            "default_interval_minutes": 15,
            "min": 10,
            "max": 60,
            "applies_after_minutes": 0,
            "message": "Hidrátate: bebe agua o bebida isotónica",
            "disclaimer": DISCLAIMER,
        },
        {
            "type": "NUTRITION",
            "default_interval_minutes": 45,
            "min": 30,
            "max": 120,
            "applies_after_minutes": 60,
            "message": "Consume un alimento ligero según tu plan",
            "disclaimer": DISCLAIMER,
        },
    ],
}
PREFERENCES = {
    "hydration": {"enabled": True, "interval_minutes": 15},
    "nutrition": {"enabled": True, "interval_minutes": 45},
    "version": 0,
}
PUT_BODY = {
    "hydration": {"enabled": True, "interval_minutes": 20},
    "nutrition": {"enabled": False, "interval_minutes": 60},
    "expected_version": 0,
}


class SessionRemindersRoutesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.upstream = SlowableUpstream(
            {"subscription_status": "ACTIVE", "capabilities": [CAPABILITY]}
        )
        self.reply: httpx.Response = httpx.Response(200, json={})
        self.failure: type[Exception] | None = None
        self.requests: list[httpx.Request] = []

        def training(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            if self.failure is not None:
                raise self.failure("entrenamientos")
            return self.reply

        settings = BffMobileSettings(
            app_env="test",  # type: ignore[arg-type]
            users_management_base_url="http://users.invalid",  # type: ignore[arg-type]
            affiliation_management_base_url="http://affiliation.invalid",  # type: ignore[arg-type]
            training_management_base_url="http://training.invalid",  # type: ignore[arg-type]
            events_management_base_url="http://events.invalid",  # type: ignore[arg-type]
            complementary_services_base_url="http://services.invalid",  # type: ignore[arg-type]
        )
        users = UsersManagementClient(
            httpx.Client(
                base_url="http://users", transport=httpx.MockTransport(self.upstream.users)
            )
        )
        affiliation = AffiliationManagementClient(
            httpx.Client(
                base_url="http://affiliation",
                transport=httpx.MockTransport(self.upstream.affiliation),
            )
        )
        verifier = CachedJwksVerifier(
            users.jwks, issuer=settings.jwt_issuer, audience=settings.jwt_audience
        )
        app = create_service_app(
            service_name="bff-mobile",
            title="SportApp - BFF móvil",
            api_version="test",
            settings=settings,
            routers=[
                build_session_reminders_router(
                    verify_token=verifier.verify,
                    athlete_snapshot=affiliation.user_capabilities,
                    training=TrainingManagementClient(
                        httpx.Client(
                            base_url="http://training", transport=httpx.MockTransport(training)
                        )
                    ),
                )
            ],
        )
        self.client = TestClient(app)
        self.logs = RecordedLogs()
        logging.getLogger().addHandler(self.logs)
        self.addCleanup(logging.getLogger().removeHandler, self.logs)

    def auth(self, actor: str = "athlete", **headers: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token(actor)}", **headers}

    def every_operation(self, headers: dict[str, str] | None = None) -> dict[str, Any]:
        return {
            "rules": self.client.get(RULES_PATH, headers=headers),
            "get": self.client.get(PREFERENCES_PATH, headers=headers),
            "put": self.client.put(PREFERENCES_PATH, json=PUT_BODY, headers=headers),
        }

    def test_rules_are_relayed_unchanged_with_the_correlation(self) -> None:
        correlation = str(uuid.uuid4())
        self.reply = httpx.Response(200, json=RULES)
        response = self.client.get(
            RULES_PATH, headers=self.auth(**{"X-Correlation-ID": correlation})
        )
        self.assertEqual((response.status_code, response.json()), (200, RULES))
        sent = self.requests[0]
        self.assertEqual((sent.method, sent.url.path), ("GET", RULES_PATH))
        self.assertEqual(sent.headers["X-Correlation-ID"], correlation)
        self.assertTrue(sent.headers["Authorization"].startswith("Bearer "))

    def test_get_preferences_is_relayed_without_cache(self) -> None:
        self.reply = httpx.Response(200, json=PREFERENCES)
        response = self.client.get(PREFERENCES_PATH, headers=self.auth())
        self.assertEqual((response.status_code, response.json()), (200, PREFERENCES))
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(self.requests[0].url.path, PREFERENCES_PATH)

    def test_put_forwards_the_body_as_is(self) -> None:
        updated = {**PREFERENCES, "version": 1}
        self.reply = httpx.Response(200, json=updated)
        body = {**PUT_BODY, "extra": True}
        response = self.client.put(PREFERENCES_PATH, json=body, headers=self.auth())
        self.assertEqual((response.status_code, response.json()), (200, updated))
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        sent = self.requests[0]
        self.assertEqual((sent.method, sent.url.path), ("PUT", PREFERENCES_PATH))
        self.assertEqual(json.loads(sent.content), body)

    def test_service_errors_pass_through(self) -> None:
        """CA5: `422 validation_error` y `409 version_conflict` llegan sin alterar."""
        cases = [
            (
                422,
                error(
                    "validation_error",
                    [{"field": "hydration.interval_minutes", "issue": "out_of_range"}],
                ),
            ),
            (409, error("version_conflict", [{"field": "expected_version", "issue": "2"}])),
            (403, error("capability_required")),
            (503, error("upstream_unavailable")),
        ]
        for status, body in cases:
            self.reply = httpx.Response(status, json=body)
            response = self.client.put(PREFERENCES_PATH, json=PUT_BODY, headers=self.auth())
            with self.subTest(status=status):
                self.assertEqual((response.status_code, response.json()), (status, body))

    def test_unexpected_service_status_is_503(self) -> None:
        self.reply = httpx.Response(409, json=error("version_conflict"))
        response = self.client.get(RULES_PATH, headers=self.auth())
        self.assertEqual(
            (response.status_code, response.json()["code"]), (503, "upstream_unavailable")
        )

    def test_without_the_capability_is_403_without_calling_the_service(self) -> None:
        self.upstream.snapshot = {"subscription_status": "ACTIVE", "capabilities": []}
        for name, response in self.every_operation(self.auth()).items():
            with self.subTest(operation=name):
                self.assertEqual(
                    (response.status_code, response.json()["code"]), (403, "capability_required")
                )
        self.upstream.snapshot = {"subscription_status": "ENDED", "capabilities": [CAPABILITY]}
        for name, response in self.every_operation(self.auth()).items():
            with self.subTest(operation=name, status="ENDED"):
                self.assertEqual(response.json()["code"], "capability_required")
        self.assertEqual(self.requests, [])

    def test_other_actor_or_no_token(self) -> None:
        for name, response in self.every_operation(self.auth("organizer")).items():
            with self.subTest(operation=name):
                self.assertEqual(
                    (response.status_code, response.json()["code"]), (403, "permission_required")
                )
        for name, response in self.every_operation().items():
            with self.subTest(operation=name, token=None):
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.headers["WWW-Authenticate"], "Bearer")
        self.assertEqual(self.requests, [])

    def test_affiliation_down_or_slow(self) -> None:
        self.upstream.status = 0
        down = self.every_operation(self.auth())
        self.upstream.status, self.upstream.slow = 200, True
        slow = self.every_operation(self.auth())
        for name in down:
            with self.subTest(operation=name):
                self.assertEqual(
                    (down[name].status_code, down[name].json()["code"]),
                    (503, "upstream_unavailable"),
                )
                self.assertEqual(
                    (slow[name].status_code, slow[name].json()["code"]), (504, "upstream_timeout")
                )
        self.assertEqual(self.requests, [])

    def test_jwks_unavailable(self) -> None:
        self.upstream.jwks_down = True
        response = self.client.get(RULES_PATH, headers=self.auth())
        self.assertEqual(
            (response.status_code, response.json()["code"]), (503, "upstream_unavailable")
        )

    def test_training_unreachable(self) -> None:
        self.failure = httpx.ReadTimeout
        timeout = self.every_operation(self.auth())
        self.failure = httpx.ConnectError
        down = self.every_operation(self.auth())
        for name in timeout:
            with self.subTest(operation=name):
                self.assertEqual(
                    (timeout[name].status_code, timeout[name].json()["code"]),
                    (504, "upstream_timeout"),
                )
                self.assertEqual(
                    (down[name].status_code, down[name].json()["code"]),
                    (503, "upstream_unavailable"),
                )

    def test_logs_never_contain_the_preference_values(self) -> None:
        self.reply = httpx.Response(200, json={**PREFERENCES, "version": 1})
        body = {
            "hydration": {"enabled": True, "interval_minutes": 37},
            "nutrition": {"enabled": True, "interval_minutes": 97},
            "expected_version": 0,
        }
        self.client.put(PREFERENCES_PATH, json=body, headers=self.auth())
        fields = [dict(getattr(record, "fields", {}) or {}) for record in self.logs.records]
        for item in fields:
            self.assertNotIn("interval_minutes", item)
            self.assertNotIn("hydration", item)
            self.assertNotIn("nutrition", item)


if __name__ == "__main__":
    unittest.main()
