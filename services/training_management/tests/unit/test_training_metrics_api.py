"""API de HU034 en el servicio: contrato de ADR-030, catálogo con `ETag`, errores e historial."""

import unittest
from typing import Any
from uuid import UUID, uuid4

from fakes import FakeCapabilities, FixedClock
from fastapi.testclient import TestClient
from metric_fakes import (
    EXTENDED_TEST_CATALOG,
    METRICS_CAPABILITY,
    InMemoryMetricResultReader,
    InMemoryMetricStore,
    InMemoryMetricUnitOfWork,
    cooper_body,
    ftp_body,
    recovery_body,
)
from test_api import VERIFIER, token

from sportapp_platform.app import create_service_app
from sportapp_platform.config import ServiceSettings
from training_management.api.training_metrics import build_training_metrics_router
from training_management.application.commands.calculate_training_metric import (
    CalculateTrainingMetric,
)
from training_management.application.errors import CapabilitiesTimeout
from training_management.application.queries.training_metrics import (
    GetMetricCatalog,
    ListTrainingMetricResults,
)
from training_management.domain.metrics.catalog import TRAINING_METRICS_CATALOG, MetricCatalog
from training_management.domain.metrics.model import TrainingMetricCalculation

CATALOG = "/v1/training-metrics/catalog"
CALCULATIONS = "/v1/training-metrics/calculations"


class SlowAffiliation(FakeCapabilities):
    def has_capability(self, user_id: Any, capability: str) -> bool:
        raise CapabilitiesTimeout()


class TrainingMetricsApiTest(unittest.TestCase):
    catalog: MetricCatalog = TRAINING_METRICS_CATALOG

    def setUp(self) -> None:
        self.store = InMemoryMetricStore()
        self.results: dict[UUID, TrainingMetricCalculation] = {}
        self.athlete_id = uuid4()
        self.capabilities = FakeCapabilities({self.athlete_id: {METRICS_CAPABILITY}})
        self.client = self.client_with(self.capabilities)
        self.auth = {"Authorization": f"Bearer {token(self.athlete_id, 'athlete')}"}

    def client_with(self, capabilities: FakeCapabilities) -> TestClient:
        app = create_service_app(
            service_name="training-management",
            title="t",
            api_version="t",
            settings=ServiceSettings(app_env="test"),
            routers=[
                build_training_metrics_router(
                    verify_token=VERIFIER.verify,
                    get_catalog=GetMetricCatalog(capabilities=capabilities, catalog=self.catalog),
                    calculate=CalculateTrainingMetric(
                        unit_of_work=lambda: InMemoryMetricUnitOfWork(self.store),
                        capabilities=capabilities,
                        catalog=self.catalog,
                        clock=FixedClock(),
                    ),
                    list_results=ListTrainingMetricResults(
                        reader=InMemoryMetricResultReader(self.results),
                        capabilities=capabilities,
                        catalog=self.catalog,
                    ),
                )
            ],
        )
        return TestClient(app)

    def calculate(self, body: Any, key: str | None = None, **headers: str) -> Any:
        sent = {**self.auth, **headers}
        if key is not None:
            sent["Idempotency-Key"] = key
        return self.client.post(CALCULATIONS, json=body, headers=sent)

    def project_store(self) -> None:
        """Simula la proyección: copia lo confirmado a la lectura."""
        self.results.update(self.store.calculations)


class CatalogApiTest(TrainingMetricsApiTest):
    def test_catalog_with_etag_and_not_modified(self) -> None:
        response = self.client.get(CATALOG, headers=self.auth)
        etag = response.headers["ETag"]
        cached = self.client.get(CATALOG, headers={**self.auth, "If-None-Match": etag})
        weak = self.client.get(CATALOG, headers={**self.auth, "If-None-Match": f'"x", W/{etag}'})
        other = self.client.get(CATALOG, headers={**self.auth, "If-None-Match": '"old.v0"'})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(etag, '"training-metrics-2026-10-12.v1"')
        document = response.json()
        self.assertEqual(document["catalog_version"], "training-metrics-2026-10-12.v1")
        self.assertEqual(
            document["methods"][0],
            {
                "metric_code": "FTP_ESTIMATED",
                "label_key": "training_metric.ftp_estimated",
                "result_unit": "W",
                "result_scale": 0,
                "method_code": "FTP_20_MIN_95_V1",
                "method_version": 1,
                "method_label_key": "training_metric.method.ftp_20_min_95_v1",
                "disclaimer_key": "estimated_not_diagnostic",
                "inputs": [
                    {
                        "code": "mean_power_watts",
                        "label_key": "training_metric.input.mean_power_watts",
                        "value_type": "INTEGER",
                        "unit": "W",
                        "minimum": 1,
                        "maximum": 2500,
                        "scale": 0,
                        "required": True,
                    }
                ],
            },
        )
        self.assertEqual(
            [(m["method_code"], m["result_unit"], m["result_scale"]) for m in document["methods"]],
            [("FTP_20_MIN_95_V1", "W", 0), ("COOPER_12_MIN_V1", "mL/kg/min", 1)],
        )
        self.assertEqual((cached.status_code, cached.content), (304, b""))
        self.assertEqual(cached.headers["ETag"], etag)
        self.assertEqual(weak.status_code, 304)
        self.assertEqual(other.status_code, 200)

    def test_catalog_access(self) -> None:
        provider = self.client.get(
            CATALOG, headers={"Authorization": f"Bearer {token(uuid4(), 'service_provider')}"}
        )
        without = self.client.get(
            CATALOG, headers={"Authorization": f"Bearer {token(uuid4(), 'athlete')}"}
        )
        anonymous = self.client.get(CATALOG)
        self.capabilities.unavailable = True
        down = self.client.get(CATALOG, headers=self.auth)

        self.assertEqual(
            (provider.status_code, provider.json()["code"]), (403, "permission_required")
        )
        self.assertEqual(
            (without.status_code, without.json()["code"]), (403, "capability_required")
        )
        self.assertEqual(
            (anonymous.status_code, anonymous.json()["code"]), (401, "invalid_access_token")
        )
        self.assertEqual((down.status_code, down.json()["code"]), (503, "upstream_unavailable"))


class CalculationApiTest(TrainingMetricsApiTest):
    def test_calculate_matches_the_adr_contract(self) -> None:
        """CA1 y CA2 por HTTP."""
        body = ftp_body()
        response = self.calculate(body, str(uuid4()))
        vo2 = self.calculate(cooper_body(), str(uuid4()))

        self.assertEqual(response.status_code, 201, response.text)
        self.assertNotIn("Idempotent-Replayed", response.headers)
        self.assertEqual(
            response.json(),
            {
                **body,
                "athlete_id": str(self.athlete_id),
                "result": {"value": 238, "unit": "W", "scale": 0},
                "status": "CONFIRMED",
                "calculated_at": "2026-10-12T12:00:00Z",
                "disclaimer_key": "estimated_not_diagnostic",
                "version": 1,
            },
        )
        self.assertEqual(vo2.json()["result"], {"value": 42.4, "unit": "mL/kg/min", "scale": 1})
        self.assertIn('"value":42.4', vo2.text)
        self.assertIn('"value":238,', response.text)

    def test_idempotent_replays_and_conflicts(self) -> None:
        """CA4: clave → `201` + `Idempotent-Replayed`; `calculation_id` → `200`; `409`."""
        body, key = ftp_body(), str(uuid4())
        first = self.calculate(body, key)
        replay = self.calculate(body, key)
        existing = self.calculate(body, str(uuid4()))
        key_reused = self.calculate(ftp_body(), key)
        id_reused = self.calculate({**body, "measured_at": "2026-10-12T11:00:00Z"}, str(uuid4()))

        self.assertEqual((replay.status_code, replay.json()), (201, first.json()))
        self.assertEqual(replay.headers["Idempotent-Replayed"], "true")
        self.assertEqual((existing.status_code, existing.json()), (200, first.json()))
        self.assertNotIn("Idempotent-Replayed", existing.headers)
        self.assertEqual(
            (key_reused.status_code, key_reused.json()["code"]), (409, "idempotency_key_reused")
        )
        self.assertEqual(
            (id_reused.status_code, id_reused.json()["code"]), (409, "calculation_id_reused")
        )
        self.assertEqual(len(self.store.outbox), 1)

    def test_validation_errors_use_the_adr_catalog(self) -> None:
        """CA3: campo y causa estables, sin persistir nada."""
        body = ftp_body(athlete_id=str(uuid4()), measured_at="2026-10-12T11:30:00+00:00")
        body["inputs"] = [
            {"code": "mean_power_watts", "value": 2501, "unit": "kW"},
            {"code": "mean_power_watts", "value": 250, "unit": "W"},
        ]

        response = self.calculate(body, str(uuid4()))
        missing_key = self.calculate(ftp_body(method_version=2))
        bad_key = self.calculate(ftp_body(), "x")
        not_json = self.client.post(
            CALCULATIONS,
            content=b"{",
            headers={
                **self.auth,
                "Idempotency-Key": str(uuid4()),
                "Content-Type": "application/json",
            },
        )

        self.assertEqual((response.status_code, response.json()["code"]), (422, "validation_error"))
        self.assertCountEqual(
            response.json()["details"],
            [
                {"field": "athlete_id", "issue": "field_not_allowed"},
                {"field": "measured_at", "issue": "invalid_datetime"},
                {"field": "inputs[0].unit", "issue": "invalid_unit"},
                {"field": "inputs[0].value", "issue": "out_of_range"},
                {"field": "inputs[1].code", "issue": "duplicate_input"},
            ],
        )
        self.assertCountEqual(
            missing_key.json()["details"],
            [
                {"field": "Idempotency-Key", "issue": "required"},
                {"field": "method_version", "issue": "unsupported_method_version"},
            ],
        )
        self.assertEqual(
            bad_key.json()["details"], [{"field": "Idempotency-Key", "issue": "invalid_uuid"}]
        )
        self.assertEqual(not_json.status_code, 422)
        self.assertEqual(
            (self.store.calculations, self.store.outbox, self.store.commands), ({}, [], {})
        )

    def test_actor_capability_and_upstream_failures(self) -> None:
        provider = self.calculate(
            ftp_body(), str(uuid4()), Authorization=f"Bearer {token(uuid4(), 'service_provider')}"
        )
        without = self.calculate(
            ftp_body(), str(uuid4()), Authorization=f"Bearer {token(uuid4(), 'athlete')}"
        )
        self.capabilities.unavailable = True
        down = self.calculate(ftp_body(), str(uuid4()))
        self.client = self.client_with(SlowAffiliation())
        slow = self.calculate(ftp_body(), str(uuid4()))

        self.assertEqual(
            (provider.status_code, provider.json()["code"]), (403, "permission_required")
        )
        self.assertEqual(
            (without.status_code, without.json()["code"]), (403, "capability_required")
        )
        self.assertEqual((down.status_code, down.json()["code"]), (503, "upstream_unavailable"))
        self.assertEqual((slow.status_code, slow.json()["code"]), (504, "upstream_timeout"))
        self.assertEqual(self.store.calculations, {})


class ResultsApiTest(TrainingMetricsApiTest):
    def test_own_projected_results_with_cursor(self) -> None:
        """CA5: historial propio ordenado y paginado; aislamiento entre deportistas."""
        bodies = [
            ftp_body(measured_at="2026-10-12T09:00:00Z"),
            cooper_body(measured_at="2026-10-12T10:00:00Z"),
            ftp_body(measured_at="2026-10-12T11:00:00Z"),
        ]
        documents = [self.calculate(body, str(uuid4())).json() for body in bodies]
        other_id = uuid4()
        self.capabilities.granted[other_id] = {METRICS_CAPABILITY}
        self.calculate(
            ftp_body(), str(uuid4()), Authorization=f"Bearer {token(other_id, 'athlete')}"
        )
        self.project_store()

        first = self.client.get(CALCULATIONS, params={"limit": 2}, headers=self.auth)
        second = self.client.get(
            CALCULATIONS,
            params={"limit": 2, "cursor": first.json()["next_cursor"]},
            headers=self.auth,
        )
        vo2 = self.client.get(
            CALCULATIONS, params={"metric_code": "VO2MAX_ESTIMATED"}, headers=self.auth
        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.headers["Cache-Control"], "no-store")
        self.assertEqual(first.json()["items"], [documents[2], documents[1]])
        self.assertEqual(second.json(), {"items": [documents[0]], "next_cursor": None})
        self.assertEqual(vo2.json()["items"], [documents[1]])

    def test_empty_collection_and_invalid_queries(self) -> None:
        empty = self.client.get(CALCULATIONS, headers=self.auth)
        unknown = self.client.get(CALCULATIONS, params={"metric_code": "X"}, headers=self.auth)
        limits = [
            self.client.get(CALCULATIONS, params={"limit": value}, headers=self.auth)
            for value in ("0", "101", "diez")
        ]
        long_code = self.client.get(
            CALCULATIONS, params={"metric_code": "X" * 200}, headers=self.auth
        )
        cursor = self.client.get(CALCULATIONS, params={"cursor": "%%%"}, headers=self.auth)
        provider = self.client.get(
            CALCULATIONS, headers={"Authorization": f"Bearer {token(uuid4(), 'service_provider')}"}
        )

        self.assertEqual(
            (empty.status_code, empty.json()), (200, {"items": [], "next_cursor": None})
        )
        self.assertEqual(
            unknown.json()["details"], [{"field": "query.metric_code", "issue": "unknown_metric"}]
        )
        for limit in limits:
            self.assertEqual(
                (limit.status_code, limit.json()["details"]),
                (422, [{"field": "query.limit", "issue": "out_of_range"}]),
                "causas de ADR-030, no las de pydantic",
            )
        self.assertEqual(
            long_code.json()["details"], [{"field": "query.metric_code", "issue": "unknown_metric"}]
        )
        self.assertEqual(
            cursor.json()["details"], [{"field": "query.cursor", "issue": "invalid_cursor"}]
        )
        self.assertEqual(provider.status_code, 403)


class ExtensibilityApiTest(TrainingMetricsApiTest):
    """HA014: un tercer método llega por las mismas rutas sin cambiarlas."""

    catalog = EXTENDED_TEST_CATALOG

    def test_third_method_in_catalog_calculation_and_history(self) -> None:
        catalog = self.client.get(CATALOG, headers=self.auth).json()
        created = self.calculate(recovery_body(), str(uuid4()))
        self.project_store()
        history = self.client.get(
            CALCULATIONS, params={"metric_code": "HR_RECOVERY_TEST"}, headers=self.auth
        )

        self.assertEqual(
            [method["method_code"] for method in catalog["methods"]][-1],
            "HR_RECOVERY_1_MIN_TEST_V1",
        )
        self.assertEqual(
            [item["code"] for item in catalog["methods"][-1]["inputs"]],
            ["peak_heart_rate_bpm", "recovery_heart_rate_bpm"],
        )
        self.assertEqual(created.status_code, 201, created.text)
        self.assertEqual(created.json()["result"], {"value": 32, "unit": "bpm", "scale": 0})
        self.assertEqual(history.json()["items"], [created.json()])


if __name__ == "__main__":
    unittest.main()
