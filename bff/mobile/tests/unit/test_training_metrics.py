"""BFF móvil, HU034: catálogo, cálculo y resultados de FTP y VO₂max (ADR-030).

El borde exige `athlete` y `training.metrics.ftp_vo2max`; lo demás llega del servicio sin
alterar: `ETag`/`304`, `Idempotent-Replayed`, códigos y `details`.
"""

import json
import unittest
import uuid
from typing import Any

import httpx
from fastapi.testclient import TestClient
from test_capabilities import FREE_CAPABILITIES, Upstream, token

from bff_mobile.main import create_app
from bff_mobile.settings import BffMobileSettings

CATALOG_VERSION = "training-metrics-2026-10-12.v1"
ETAG = f'"{CATALOG_VERSION}"'
CATALOG = {
    "catalog_version": CATALOG_VERSION,
    "methods": [
        {
            "metric_code": "FTP_ESTIMATED",
            "label_key": "metric.ftp_estimated",
            "result_unit": "W",
            "result_scale": 0,
            "method_code": "FTP_20_MIN_95_V1",
            "method_version": 1,
            "method_label_key": "method.ftp_20_min_95",
            "disclaimer_key": "estimated_not_diagnostic",
            "inputs": [
                {
                    "code": "mean_power_watts",
                    "label_key": "input.mean_power_watts",
                    "value_type": "INTEGER",
                    "unit": "W",
                    "minimum": 1,
                    "maximum": 2500,
                    "scale": 0,
                    "required": True,
                }
            ],
        }
    ],
}
CALCULATION_ID = str(uuid.uuid4())
REQUEST = {
    "calculation_id": CALCULATION_ID,
    "metric_code": "FTP_ESTIMATED",
    "method_code": "FTP_20_MIN_95_V1",
    "method_version": 1,
    "measured_at": "2026-10-12T15:30:00Z",
    "inputs": [{"code": "mean_power_watts", "value": 250, "unit": "W"}],
}
CALCULATION = {
    **REQUEST,
    "athlete_id": str(uuid.uuid4()),
    "result": {"value": 238, "unit": "W", "scale": 0},
    "status": "CONFIRMED",
    "calculated_at": "2026-10-12T15:31:02Z",
    "disclaimer_key": "estimated_not_diagnostic",
    "version": 1,
}
COOPER = {
    **CALCULATION,
    "calculation_id": str(uuid.uuid4()),
    "metric_code": "VO2MAX_ESTIMATED",
    "method_code": "COOPER_12_MIN_V1",
    "inputs": [{"code": "distance_meters", "value": 2400, "unit": "m"}],
    "result": {"value": 42.4, "unit": "mL/kg/min", "scale": 1},
}


class SlowableUpstream(Upstream):
    """Afiliación que además puede agotar el tiempo de espera."""

    slow = False

    def affiliation(self, request: httpx.Request) -> httpx.Response:
        if self.slow:
            raise httpx.ReadTimeout("afiliación lenta")
        return super().affiliation(request)


class TrainingMetricsRoutesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.upstream = SlowableUpstream(
            {"subscription_status": "ACTIVE", "capabilities": FREE_CAPABILITIES}
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
                events_http=httpx.Client(
                    base_url="http://events",
                    transport=httpx.MockTransport(lambda _: httpx.Response(500)),
                ),
            )
        )

    def auth(self, actor: str = "athlete", **headers: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token(actor)}", **headers}

    def calculate(self, **headers: str) -> httpx.Response:
        return self.client.post(
            "/v1/training-metrics/calculations", json=REQUEST, headers=self.auth(**headers)
        )

    def test_catalog_returns_body_and_etag(self) -> None:
        """CA1/CA7: catálogo con `ETag`; sin `If-None-Match` no se envía la cabecera."""
        correlation = str(uuid.uuid4())
        self.reply = httpx.Response(200, json=CATALOG, headers={"ETag": ETAG})

        response = self.client.get(
            "/v1/training-metrics/catalog", headers=self.auth(**{"X-Correlation-ID": correlation})
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), CATALOG)
        self.assertEqual(response.headers["ETag"], ETAG)
        sent = self.requests[0]
        self.assertEqual((sent.method, sent.url.path), ("GET", "/v1/training-metrics/catalog"))
        self.assertNotIn("If-None-Match", sent.headers)
        self.assertEqual(sent.headers["X-Correlation-ID"], correlation)
        self.assertTrue(sent.headers["Authorization"].startswith("Bearer "))

    def test_catalog_not_modified_is_relayed_without_body(self) -> None:
        self.reply = httpx.Response(304, headers={"ETag": ETAG})

        response = self.client.get(
            "/v1/training-metrics/catalog", headers=self.auth(**{"If-None-Match": ETAG})
        )

        self.assertEqual(response.status_code, 304)
        self.assertEqual(response.content, b"")
        self.assertEqual(response.headers["ETag"], ETAG)
        self.assertEqual(self.requests[0].headers["If-None-Match"], ETAG)

    def test_calculate_forwards_body_key_and_correlation(self) -> None:
        """CA2/CA3: cuerpo y clave llegan tal cual; el BFF no calcula ni añade `athlete_id`."""
        key, correlation = str(uuid.uuid4()), str(uuid.uuid4())
        self.reply = httpx.Response(201, json=CALCULATION)

        response = self.calculate(**{"Idempotency-Key": key, "X-Correlation-ID": correlation})

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json(), CALCULATION)
        self.assertNotIn("Idempotent-Replayed", response.headers)
        sent = self.requests[0]
        self.assertEqual(
            (sent.method, sent.url.path), ("POST", "/v1/training-metrics/calculations")
        )
        self.assertEqual(json.loads(sent.content), REQUEST)
        self.assertEqual(sent.headers["Idempotency-Key"], key)
        self.assertEqual(sent.headers["X-Correlation-ID"], correlation)

    def test_replays_are_relayed_as_the_service_answers(self) -> None:
        """Misma clave → `201` con `Idempotent-Replayed`; mismo `calculation_id` → `200`."""
        self.reply = httpx.Response(201, json=CALCULATION, headers={"Idempotent-Replayed": "true"})
        replayed = self.calculate(**{"Idempotency-Key": str(uuid.uuid4())})
        self.reply = httpx.Response(200, json=COOPER)
        existing = self.calculate(**{"Idempotency-Key": str(uuid.uuid4())})

        self.assertEqual(replayed.status_code, 201)
        self.assertEqual(replayed.headers["Idempotent-Replayed"], "true")
        self.assertEqual((existing.status_code, existing.json()), (200, COOPER))
        self.assertEqual(existing.json()["result"]["value"], 42.4)

    def test_body_is_not_reinterpreted(self) -> None:
        """Un `athlete_id` del cliente llega al servicio, que responde `field_not_allowed`."""
        rejection = {
            "code": "validation_error",
            "message": "m",
            "correlation_id": "c",
            "details": [{"field": "athlete_id", "issue": "field_not_allowed"}],
        }
        self.reply = httpx.Response(422, json=rejection)
        body = {**REQUEST, "athlete_id": str(uuid.uuid4())}

        response = self.client.post(
            "/v1/training-metrics/calculations", json=body, headers=self.auth()
        )

        self.assertEqual((response.status_code, response.json()), (422, rejection))
        self.assertEqual(json.loads(self.requests[0].content), body)
        self.assertNotIn("Idempotency-Key", self.requests[0].headers, "sin clave no se inventa")

    def test_service_errors_pass_through_unchanged(self) -> None:
        """CA4/CA5: estado, código y `details` del servicio sin alterar."""
        cases: list[tuple[int, dict[str, Any]]] = [
            (409, {"code": "idempotency_key_reused"}),
            (409, {"code": "calculation_id_reused"}),
            (
                422,
                {
                    "code": "validation_error",
                    "details": [{"field": "inputs[0].value", "issue": "out_of_range"}],
                },
            ),
            (
                422,
                {
                    "code": "validation_error",
                    "details": [{"field": "measured_at", "issue": "too_far_in_future"}],
                },
            ),
            (403, {"code": "capability_required"}),
            (401, {"code": "invalid_access_token"}),
            (503, {"code": "upstream_unavailable"}),
            (504, {"code": "upstream_timeout"}),
        ]
        for status, body in cases:
            self.reply = httpx.Response(
                status, json={"message": "m", "correlation_id": "c", "details": [], **body}
            )
            with self.subTest(status=status, code=body["code"]):
                response = self.calculate(**{"Idempotency-Key": str(uuid.uuid4())})
                self.assertEqual(response.status_code, status)
                self.assertEqual(response.json()["code"], body["code"])
                self.assertEqual(response.json()["details"], body.get("details", []))

    def test_calculations_forward_only_present_params(self) -> None:
        """CA7: resultados propios; parámetros tal cual y `no-store`."""
        page = {"items": [COOPER, CALCULATION], "next_cursor": "opaco"}
        self.reply = httpx.Response(200, json=page)
        listed = self.client.get(
            "/v1/training-metrics/calculations?metric_code=FTP_ESTIMATED&limit=5&cursor=abc",
            headers=self.auth(),
        )
        self.reply = httpx.Response(200, json={"items": [], "next_cursor": None})
        unfiltered = self.client.get("/v1/training-metrics/calculations", headers=self.auth())
        self.reply = httpx.Response(
            422,
            json={
                "code": "validation_error",
                "message": "m",
                "correlation_id": "c",
                "details": [{"field": "query.metric_code", "issue": "unknown_metric"}],
            },
        )
        unknown = self.client.get(
            "/v1/training-metrics/calculations?metric_code=X", headers=self.auth()
        )

        self.assertEqual((listed.status_code, listed.json()), (200, page))
        self.assertEqual(listed.headers["Cache-Control"], "no-store")
        self.assertEqual(
            dict(self.requests[0].url.params),
            {"metric_code": "FTP_ESTIMATED", "limit": "5", "cursor": "abc"},
        )
        self.assertEqual(unfiltered.json(), {"items": [], "next_cursor": None})
        self.assertEqual(dict(self.requests[1].url.params), {})
        self.assertEqual(
            (unknown.status_code, unknown.json()["details"][0]["issue"]), (422, "unknown_metric")
        )

    def test_edge_rejections_never_reach_the_service(self) -> None:
        """CA5: otro actor, sin token, token inválido o sin capacidad: no se llama al servicio."""
        organizer = self.client.get("/v1/training-metrics/catalog", headers=self.auth("organizer"))
        provider = self.client.post(
            "/v1/training-metrics/calculations",
            json=REQUEST,
            headers=self.auth("service_provider"),
        )
        anonymous = self.client.get("/v1/training-metrics/calculations")
        invalid = self.client.get(
            "/v1/training-metrics/catalog", headers={"Authorization": "Bearer roto"}
        )
        affiliation_calls = len(self.upstream.affiliation_paths)
        self.upstream.snapshot = {
            "subscription_status": "ACTIVE",
            "capabilities": [c for c in FREE_CAPABILITIES if c != "training.metrics.ftp_vo2max"],
        }
        without = self.calculate()
        self.upstream.snapshot = {"subscription_status": "NONE", "capabilities": []}
        inactive = self.client.get("/v1/training-metrics/calculations", headers=self.auth())

        for response in (organizer, provider):
            self.assertEqual(
                (response.status_code, response.json()["code"]), (403, "permission_required")
            )
        self.assertEqual(affiliation_calls, 0, "otro actor no consulta Afiliación")
        self.assertEqual(anonymous.status_code, 401)
        self.assertEqual(invalid.json()["code"], "invalid_access_token")
        for response in (without, inactive):
            self.assertEqual(
                (response.status_code, response.json()["code"]), (403, "capability_required")
            )
        self.assertEqual(self.requests, [])

    def test_affiliation_down_or_slow(self) -> None:
        self.upstream.status = 0
        down = self.client.get("/v1/training-metrics/catalog", headers=self.auth())
        self.upstream.status, self.upstream.slow = 200, True
        timeout = self.calculate()

        self.assertEqual((down.status_code, down.json()["code"]), (503, "upstream_unavailable"))
        self.assertEqual((timeout.status_code, timeout.json()["code"]), (504, "upstream_timeout"))
        self.assertEqual(self.requests, [])

    def test_upstream_failures(self) -> None:
        self.failure = httpx.ReadTimeout
        timeout = self.calculate()
        self.failure = httpx.ConnectError
        down = self.client.get("/v1/training-metrics/catalog", headers=self.auth())
        self.failure = None
        self.reply = httpx.Response(500, json={"code": "internal_error"})
        unexpected = self.client.get("/v1/training-metrics/calculations", headers=self.auth())
        self.reply = httpx.Response(404, json={"code": "not_found"})
        not_found = self.client.get("/v1/training-metrics/catalog", headers=self.auth())
        self.reply = httpx.Response(200, content=b"no es json")
        garbage = self.client.get("/v1/training-metrics/catalog", headers=self.auth())

        self.assertEqual((timeout.status_code, timeout.json()["code"]), (504, "upstream_timeout"))
        self.assertEqual((down.status_code, down.json()["code"]), (503, "upstream_unavailable"))
        for response in (unexpected, not_found, garbage):
            self.assertEqual(
                (response.status_code, response.json()["code"]), (503, "upstream_unavailable")
            )
        self.assertEqual(unexpected.headers["Cache-Control"], "no-store")


if __name__ == "__main__":
    unittest.main()
