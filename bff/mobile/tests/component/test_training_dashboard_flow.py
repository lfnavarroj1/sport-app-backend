"""Componente HU033 en móvil: BFF → Usuarios (sesión y JWKS) → Afiliación (capacidad
`training.dashboard.view` del plan `FREE`) → Entrenamientos → outbox → Pub/Sub →
`entrenamientos-read-projector` → tablero (ADR-043).

Recorrido de la evidencia de ADR-043 en el backend: el deportista finaliza dos sesiones (HU032)
y calcula una FTP (HU034) por `bff-mobile`; una vez proyectadas, el tablero muestra totales, dos
barras por deporte y la métrica. Mismo montaje que `test_training_metrics_flow.py` (servicios
reales con PostgreSQL migrado y el emulador de Pub/Sub con un proyecto propio).
"""

import os
import unittest
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import test_training_metrics_flow as metrics_flow
from test_training_metrics_flow import ftp, utc_seconds

from training_management.workers.pubsub_setup import ensure_resources

DASHBOARD = "/v1/me/training-dashboard"


class TrainingDashboardFlowTest(unittest.TestCase):
    # Se reutiliza el montaje de HU034 sin heredar sus pruebas (se importa el módulo, no la
    # clase, para que `unittest` no vuelva a descubrirlas aquí).
    setUp = metrics_flow.TrainingMetricsFlowTest.setUp
    register = metrics_flow.TrainingMetricsFlowTest.register
    calculate = metrics_flow.TrainingMetricsFlowTest.calculate
    publish = metrics_flow.TrainingMetricsFlowTest.publish
    consume = metrics_flow.TrainingMetricsFlowTest.consume

    def dashboard(
        self, user: dict[str, str], params: dict[str, str] | None = None
    ) -> httpx.Response:
        return self.bff.get(
            DASHBOARD, params=params, headers={"Authorization": user["Authorization"]}
        )

    def train(self, user: dict[str, str], sport: str, started: timedelta, minutes: int) -> None:
        session_id = str(uuid.uuid4())
        base = datetime.now(UTC)
        start = self.bff.post(
            "/v1/training-sessions",
            json={
                "session_id": session_id,
                "sport": sport,
                "started_at": utc_seconds(started, base),
            },
            headers={"Authorization": user["Authorization"], "Idempotency-Key": str(uuid.uuid4())},
        )
        self.assertEqual(start.status_code, 201, start.text)
        finish = self.bff.post(
            f"/v1/training-sessions/{session_id}/finish",
            json={"ended_at": utc_seconds(started + timedelta(minutes=minutes), base)},
            headers={"Authorization": user["Authorization"], "Idempotency-Key": str(uuid.uuid4())},
        )
        self.assertEqual(finish.status_code, 200, finish.text)

    @unittest.skipUnless(os.environ.get("PUBSUB_EMULATOR_HOST"), "requiere el emulador de Pub/Sub")
    def test_two_sessions_and_an_ftp_reach_the_dashboard(self) -> None:
        """CA1-CA3: totales, dos deportes, semana ISO, fuente y la FTP con su método."""
        project_id = f"sportapp-test-{uuid.uuid4().hex[:8]}"
        ensure_resources(project_id)
        athlete, other = self.register("athlete"), self.register("athlete")
        empty = self.dashboard(athlete)
        self.train(athlete, "running", -timedelta(hours=3), 40)
        self.train(athlete, "cycling", -timedelta(hours=2), 50)
        self.train(other, "swimming", -timedelta(hours=2), 30)
        created = self.calculate(
            athlete, ftp(250, utc_seconds(-timedelta(hours=1))), str(uuid.uuid4())
        )
        self.assertEqual(created.status_code, 201, created.text)

        self.publish(project_id)
        projected = self.consume(
            project_id,
            lambda: (
                self.dashboard(athlete).json()["totals"]["sessions_count"] == 2
                and len(self.dashboard(athlete).json()["latest_metrics"]) == 1
            ),
        )

        self.assertEqual(empty.status_code, 200, empty.text)
        self.assertFalse(empty.json()["has_data"])
        self.assertEqual(empty.headers["Cache-Control"], "no-store")
        self.assertTrue(projected, "el proyector no llevó las sesiones y la FTP al tablero")
        body: dict[str, Any] = self.dashboard(athlete).json()
        self.assertTrue(body["has_data"])
        self.assertEqual(
            body["totals"],
            {
                "sessions_count": 2,
                "total_duration_minutes": 90,
                "avg_duration_minutes": 45,
                "distance_meters": None,
            },
        )
        self.assertEqual(
            [(item["sport"], item["total_duration_minutes"]) for item in body["by_sport"]],
            [("running", 40), ("cycling", 50)],
        )
        self.assertEqual(sum(item["sessions_count"] for item in body["by_week"]), 2)
        self.assertEqual(body["by_source"]["SPORTAPP"]["sessions_count"], 2)
        self.assertEqual(body["by_source"]["STRAVA"]["sessions_count"], 0)
        [metric] = body["latest_metrics"]
        self.assertEqual(
            (metric["indicator"], metric["value"], metric["unit"], metric["method_code"]),
            ("FTP_ESTIMATED", 238, "W", "FTP_20_MIN_95_V1"),
        )
        self.assertEqual(metric["disclaimer_key"], "estimated_not_diagnostic")
        self.assertEqual(self.dashboard(other).json()["totals"]["sessions_count"], 1)

    def test_actor_capability_and_period_are_enforced(self) -> None:
        """CA4: otro actor → `403`, sin plan → `capability_required`, periodo inválido → `422`."""
        provider = self.register("service_provider")
        no_plan = self.register("athlete", free_plan=False)
        athlete = self.register("athlete")

        forbidden = self.dashboard(provider)
        no_capability = self.dashboard(no_plan)
        too_long = self.dashboard(
            athlete, {"from": "2025-01-01T00:00:00-05:00", "to": "2026-01-03T00:00:00-05:00"}
        )
        no_offset = self.dashboard(
            athlete, {"from": "2026-10-01T00:00:00", "to": "2026-10-08T00:00:00Z"}
        )

        self.assertEqual(
            (forbidden.status_code, forbidden.json()["code"]), (403, "permission_required")
        )
        self.assertEqual(
            (no_capability.status_code, no_capability.json()["code"]), (403, "capability_required")
        )
        self.assertEqual(
            (too_long.status_code, too_long.json()["details"]),
            (422, [{"field": "query.to", "issue": "out_of_range"}]),
        )
        self.assertEqual(
            no_offset.json()["details"], [{"field": "query.from", "issue": "invalid_datetime"}]
        )


if __name__ == "__main__":
    unittest.main()
