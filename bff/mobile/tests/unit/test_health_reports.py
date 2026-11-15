"""BFF móvil, HU042: reportar una molestia, incapacidad o lesión (ADR-037).

El borde exige `athlete` (sin capacidad de plan: «Mi salud» es opción base); lo demás llega
del servicio sin alterar: `Idempotent-Replayed`, códigos y `details`.
"""

import json
import logging
import unittest
import uuid

import httpx
from fastapi.testclient import TestClient
from test_capabilities import Upstream, token

from bff_mobile.main import create_app
from bff_mobile.settings import BffMobileSettings

REPORTS = "/v1/me/health-reports"
REQUEST = {
    "report_id": str(uuid.uuid4()),
    "type": "INJURY",
    "body_area": "ANKLE_FOOT",
    "severity": "HIGH",
    "occurred_at": "2026-10-12T15:30:00Z",
    "session_id": None,
    "expected_recovery_days": 14,
    "consent": {"version": "local-dev"},
}
REPORT = {
    **REQUEST,
    "athlete_id": str(uuid.uuid4()),
    "status": "OPEN",
    "reported_at": "2026-10-12T15:31:02Z",
    "version": 1,
}


class HealthReportsRoutesTest(unittest.TestCase):
    def setUp(self) -> None:
        # Sin suscripción: la opción base no consulta capacidades en Afiliación.
        self.upstream = Upstream()
        self.reply: httpx.Response = httpx.Response(201, json=REPORT)
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
            third_party_management_base_url="http://third-party.invalid",  # type: ignore[arg-type]
        )
        self.client = TestClient(
            create_app(
                settings,
                users_http=httpx.Client(
                    base_url="http://users", transport=httpx.MockTransport(self.upstream.users)
                ),
                affiliation_http=httpx.Client(
                    base_url="http://affiliation",
                    transport=httpx.MockTransport(self.upstream.affiliation),
                ),
                training_http=httpx.Client(
                    base_url="http://training", transport=httpx.MockTransport(training)
                ),
            )
        )

    def report(
        self, actor: str = "athlete", body: object = REQUEST, **headers: str
    ) -> httpx.Response:
        sent = {"Authorization": f"Bearer {token(actor)}", **headers}
        return self.client.post(REPORTS, json=body, headers=sent)

    def test_forwards_body_key_and_correlation_without_capabilities(self) -> None:
        """CA1: cuerpo y clave llegan tal cual; el BFF no añade `athlete_id` ni consulta plan."""
        key, correlation = str(uuid.uuid4()), str(uuid.uuid4())

        response = self.report(**{"Idempotency-Key": key, "X-Correlation-ID": correlation})

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json(), REPORT)
        sent = self.requests[0]
        self.assertEqual((sent.method, sent.url.path), ("POST", REPORTS))
        self.assertEqual(json.loads(sent.content), REQUEST)
        self.assertEqual(sent.headers["Idempotency-Key"], key)
        self.assertEqual(sent.headers["X-Correlation-ID"], correlation)
        self.assertTrue(sent.headers["Authorization"].startswith("Bearer "))
        self.assertEqual(self.upstream.affiliation_paths, [])

    def test_replay_header_and_cache_control_are_relayed(self) -> None:
        """CA3."""
        self.reply = httpx.Response(
            201, json=REPORT, headers={"Idempotent-Replayed": "true", "Cache-Control": "no-store"}
        )

        response = self.report(**{"Idempotency-Key": str(uuid.uuid4())})

        self.assertEqual(response.headers["Idempotent-Replayed"], "true")
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_service_errors_are_relayed_unchanged(self) -> None:
        """CA2/CA3: `404`, `409` y `422` del servicio sin alterar."""
        for status, code, details in (
            (404, "training_session_not_found", []),
            (409, "report_id_reused", []),
            (409, "idempotency_key_reused", []),
            (422, "validation_error", [{"field": "athlete_id", "issue": "field_not_allowed"}]),
            (403, "permission_required", []),
        ):
            error = {"code": code, "message": "m", "correlation_id": None, "details": details}
            self.reply = httpx.Response(status, json=error)
            with self.subTest(code=code):
                response = self.report(body={**REQUEST, "athlete_id": str(uuid.uuid4())})
                self.assertEqual((response.status_code, response.json()), (status, error))

    def test_only_athletes_reach_the_service(self) -> None:
        for actor in ("service_provider", "organizer"):
            with self.subTest(actor=actor):
                response = self.report(actor)
                self.assertEqual(response.status_code, 403)
                self.assertEqual(response.json()["code"], "permission_required")
        self.assertEqual(self.requests, [])

    def test_requires_a_valid_token(self) -> None:
        missing = self.client.post(REPORTS, json=REQUEST)
        invalid = self.client.post(REPORTS, json=REQUEST, headers={"Authorization": "Bearer x"})

        self.assertEqual((missing.status_code, invalid.status_code), (401, 401))
        self.assertEqual(missing.headers["WWW-Authenticate"], "Bearer")
        self.assertEqual(self.requests, [])

    def test_unavailable_identity_or_service(self) -> None:
        self.failure = httpx.ConnectError
        down = self.report()
        self.failure = httpx.ReadTimeout
        slow = self.report()
        self.failure = None
        self.reply = httpx.Response(500, json={"code": "boom"})
        unexpected = self.report()

        self.assertEqual((down.status_code, down.json()["code"]), (503, "upstream_unavailable"))
        self.assertEqual((slow.status_code, slow.json()["code"]), (504, "upstream_timeout"))
        self.assertEqual(unexpected.status_code, 503)

    def test_identity_not_verifiable(self) -> None:
        self.upstream.jwks_down = True

        response = self.report()

        self.assertEqual(
            (response.status_code, response.json()["code"]), (503, "upstream_unavailable")
        )
        self.assertEqual(self.requests, [])

    def test_logs_do_not_contain_health_data(self) -> None:
        with self.assertLogs(level=logging.DEBUG) as logs:
            self.report()
            self.reply = httpx.Response(500, json={})
            self.report()

        captured = " ".join(
            f"{record.getMessage()} {record.__dict__.get('fields', '')}" for record in logs.records
        )
        for value in ("INJURY", "ANKLE_FOOT", "HIGH", "15:30:00", "local-dev"):
            self.assertNotIn(value, captured)


if __name__ == "__main__":
    unittest.main()
