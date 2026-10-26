"""API de HU033 en el servicio (ADR-043): tablero propio con el token y la capacidad verificados
de verdad, un lector en memoria que agrupa sesiones como la consulta SQL, errores `403`/`422`/
`503`/`504`, fuentes parciales y `no-store`."""

import unittest
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from fakes import FakeCapabilities, FixedClock
from fastapi.testclient import TestClient
from test_api import VERIFIER, token

from sportapp_platform.app import create_service_app
from sportapp_platform.config import ServiceSettings
from training_management.api.training_dashboard import build_training_dashboard_router
from training_management.application.errors import CapabilitiesTimeout, DashboardSourceUnavailable
from training_management.application.queries.training_dashboard import GetTrainingDashboard
from training_management.domain.dashboard.model import (
    DashboardPeriod,
    LatestMetric,
    SessionBucket,
    Source,
    iso_week_start,
)

DASHBOARD = "/v1/me/training-dashboard"
CAPABILITY = "training.dashboard.view"
NOW = datetime(2026, 10, 24, 15, 0, tzinfo=UTC)


@dataclass(frozen=True)
class FinishedSession:
    athlete_id: UUID
    sport: str
    ended_at: datetime
    duration_seconds: int


@dataclass(frozen=True)
class MetricResult:
    athlete_id: UUID
    metric: LatestMetric


class InMemoryDashboardReader:
    """Misma semántica que `SqlAlchemyDashboardReader`: `[from, to)` por `ended_at` /
    `measured_at`, semana ISO en la zona del `from` y último resultado por indicador."""

    def __init__(self) -> None:
        self.sessions: list[FinishedSession] = []
        self.metrics: list[MetricResult] = []
        self.sessions_down = False
        self.metrics_down = False
        self.queried: list[UUID] = []

    def session_buckets(self, athlete_id: UUID, period: DashboardPeriod) -> list[SessionBucket]:
        self.queried.append(athlete_id)
        if self.sessions_down:
            raise DashboardSourceUnavailable()
        groups: dict[tuple[str, Any], list[int]] = {}
        for item in self.sessions:
            if item.athlete_id == athlete_id and period.start <= item.ended_at < period.end:
                key = (item.sport, iso_week_start(item.ended_at, period.offset))
                groups.setdefault(key, []).append(item.duration_seconds)
        return [
            SessionBucket(
                sport=sport,
                week_start=week,
                source=Source.SPORTAPP,
                sessions_count=len(durations),
                duration_seconds=sum(durations),
            )
            for (sport, week), durations in groups.items()
        ]

    def latest_metrics(self, athlete_id: UUID, period: DashboardPeriod) -> list[LatestMetric]:
        if self.metrics_down:
            raise DashboardSourceUnavailable()
        latest: dict[str, LatestMetric] = {}
        for item in self.metrics:
            metric = item.metric
            if item.athlete_id != athlete_id or not period.start <= metric.measured_at < period.end:
                continue
            current = latest.get(metric.indicator)
            if current is None or metric.measured_at > current.measured_at:
                latest[metric.indicator] = metric
        return list(latest.values())


class SlowAffiliation(FakeCapabilities):
    def has_capability(self, user_id: Any, capability: str) -> bool:
        raise CapabilitiesTimeout()


def metric(indicator: str, value: str, unit: str, measured_at: datetime) -> LatestMetric:
    return LatestMetric(
        indicator=indicator,
        value=Decimal(value),
        unit=unit,
        method_code=f"{indicator}_METHOD",
        method_version=1,
        measured_at=measured_at,
        disclaimer_key="estimated_not_diagnostic",
    )


class TrainingDashboardApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.athlete_id = uuid4()
        self.reader = InMemoryDashboardReader()
        self.capabilities = FakeCapabilities({self.athlete_id: {CAPABILITY}})
        self.client = self.client_with(self.capabilities)
        self.auth = {"Authorization": f"Bearer {token(self.athlete_id, 'athlete')}"}

    def client_with(self, capabilities: FakeCapabilities) -> TestClient:
        app = create_service_app(
            service_name="training-management",
            title="t",
            api_version="t",
            settings=ServiceSettings(app_env="test"),
            routers=[
                build_training_dashboard_router(
                    verify_token=VERIFIER.verify,
                    get_dashboard=GetTrainingDashboard(
                        reader=self.reader, capabilities=capabilities, clock=FixedClock(NOW)
                    ),
                )
            ],
        )
        return TestClient(app)

    def get(self, params: dict[str, str] | None = None, **headers: str) -> Any:
        return self.client.get(DASHBOARD, params=params, headers={**self.auth, **headers})

    def finished(
        self, sport: str, ended_at: datetime, minutes: int, athlete: UUID | None = None
    ) -> None:
        self.reader.sessions.append(
            FinishedSession(athlete or self.athlete_id, sport, ended_at, minutes * 60)
        )

    def details(self, response: Any) -> list[tuple[str, str]]:
        return [(item["field"], item["issue"]) for item in response.json()["details"]]

    def test_period_with_sessions_and_metrics(self) -> None:
        """CA1: totales, deporte, semana ISO, fuente y último resultado por indicador."""
        bogota = "2026-10-01T00:00:00-05:00"
        self.finished("running", datetime(2026, 10, 6, 12, tzinfo=UTC), 45)
        self.finished("running", datetime(2026, 10, 12, 4, 30, tzinfo=UTC), 30)  # dom 11 local
        self.finished("swimming", datetime(2026, 10, 14, 12, tzinfo=UTC), 60)
        older = datetime(2026, 10, 5, 12, tzinfo=UTC)
        newer = datetime(2026, 10, 15, 12, tzinfo=UTC)
        self.reader.metrics = [
            MetricResult(self.athlete_id, metric("FTP_ESTIMATED", "230", "W", older)),
            MetricResult(self.athlete_id, metric("FTP_ESTIMATED", "238", "W", newer)),
            MetricResult(self.athlete_id, metric("VO2MAX_ESTIMATED", "42.4", "mL/kg/min", older)),
        ]

        response = self.get({"from": bogota, "to": "2026-11-01T00:00:00-05:00"})

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        body = response.json()
        self.assertEqual(
            body["period"], {"from": "2026-10-01T00:00:00-05:00", "to": "2026-11-01T00:00:00-05:00"}
        )
        self.assertTrue(body["has_data"])
        self.assertEqual(
            body["totals"],
            {
                "sessions_count": 3,
                "total_duration_minutes": 135,
                "avg_duration_minutes": 45,
                "distance_meters": None,
            },
        )
        self.assertEqual(
            body["by_sport"],
            [
                {
                    "sport": "running",
                    "sessions_count": 2,
                    "total_duration_minutes": 75,
                    "distance_meters": None,
                },
                {
                    "sport": "swimming",
                    "sessions_count": 1,
                    "total_duration_minutes": 60,
                    "distance_meters": None,
                },
            ],
        )
        self.assertEqual(
            body["by_week"],
            [
                {"week_start": "2026-10-05", "sessions_count": 2, "total_duration_minutes": 75},
                {"week_start": "2026-10-12", "sessions_count": 1, "total_duration_minutes": 60},
            ],
        )
        self.assertEqual(
            body["by_source"],
            {
                "SPORTAPP": {"sessions_count": 3, "total_duration_minutes": 135},
                "STRAVA": {"sessions_count": 0, "total_duration_minutes": 0},
            },
        )
        self.assertEqual(
            body["latest_metrics"],
            [
                {
                    "indicator": "FTP_ESTIMATED",
                    "value": 238,
                    "unit": "W",
                    "method_code": "FTP_ESTIMATED_METHOD",
                    "method_version": 1,
                    "measured_at": "2026-10-15T12:00:00Z",
                    "disclaimer_key": "estimated_not_diagnostic",
                },
                {
                    "indicator": "VO2MAX_ESTIMATED",
                    "value": 42.4,
                    "unit": "mL/kg/min",
                    "method_code": "VO2MAX_ESTIMATED_METHOD",
                    "method_version": 1,
                    "measured_at": "2026-10-05T12:00:00Z",
                    "disclaimer_key": "estimated_not_diagnostic",
                },
            ],
        )
        self.assertEqual(
            body["sources"],
            [
                {"source": "training_sessions", "status": "OK"},
                {"source": "training_metrics", "status": "OK"},
            ],
        )

    def test_the_same_sessions_fall_in_other_weeks_in_another_zone(self) -> None:
        """CA3: en `+09:00` el lunes 01:00 local abre la semana siguiente."""
        self.finished("running", datetime(2026, 10, 11, 16, tzinfo=UTC), 40)  # lun 12 en Tokio

        tokyo = self.get({"from": "2026-10-01T00:00:00+09:00", "to": "2026-10-31T00:00:00+09:00"})
        utc = self.get({"from": "2026-10-01T00:00:00Z", "to": "2026-10-31T00:00:00Z"})

        self.assertEqual(tokyo.json()["by_week"][0]["week_start"], "2026-10-12")
        self.assertEqual(utc.json()["by_week"][0]["week_start"], "2026-10-05")

    def test_without_data_the_response_is_empty_not_invented(self) -> None:
        """CA2: `has_data: false`, totales en cero y listas vacías; por defecto 30 días."""
        self.finished("running", NOW - timedelta(days=31), 45)  # fuera del periodo por defecto

        body = self.get().json()

        self.assertFalse(body["has_data"])
        self.assertEqual(
            body["totals"],
            {
                "sessions_count": 0,
                "total_duration_minutes": 0,
                "avg_duration_minutes": 0,
                "distance_meters": None,
            },
        )
        self.assertEqual((body["by_sport"], body["by_week"], body["latest_metrics"]), ([], [], []))
        self.assertEqual(
            body["period"], {"from": "2026-09-24T15:00:00Z", "to": "2026-10-24T15:00:00Z"}
        )

    def test_only_the_caller_data_is_aggregated(self) -> None:
        """CA4: el tablero solo responde con los datos del `athlete_id` del token."""
        other = uuid4()
        self.finished("running", NOW - timedelta(days=1), 45, athlete=other)
        self.reader.metrics = [
            MetricResult(other, metric("FTP_ESTIMATED", "300", "W", NOW - timedelta(days=1)))
        ]

        body = self.get({"athlete_id": str(other)}).json()

        self.assertFalse(body["has_data"])
        self.assertEqual(self.reader.queried, [self.athlete_id])

    def test_other_actors_or_without_capability_are_forbidden(self) -> None:
        """CA4: otro actor → `403 permission_required`; sin capacidad → `capability_required`."""
        for actor in ("organizer", "service_provider"):
            with self.subTest(actor=actor):
                response = self.client.get(
                    DASHBOARD, headers={"Authorization": f"Bearer {token(uuid4(), actor)}"}
                )
                self.assertEqual(
                    (response.status_code, response.json()["code"]), (403, "permission_required")
                )
        without = self.client_with(FakeCapabilities()).get(DASHBOARD, headers=self.auth)
        self.assertEqual(
            (without.status_code, without.json()["code"]), (403, "capability_required")
        )
        self.assertEqual(self.client.get(DASHBOARD).status_code, 401)
        self.assertEqual(self.reader.queried, [])

    def test_invalid_periods_are_422_before_asking_affiliation(self) -> None:
        """CA4: periodo inválido o mayor de 366 días → `422 validation_error`."""
        cases = (
            ({"from": "2026-10-01T00:00:00Z"}, [("query.to", "required")]),
            ({"to": "2026-10-01T00:00:00Z"}, [("query.from", "required")]),
            (
                {"from": "2026-10-01", "to": "2026-10-08T00:00:00Z"},
                [("query.from", "invalid_datetime")],
            ),
            (
                {"from": "2026-10-08T00:00:00Z", "to": "2026-10-01T00:00:00Z"},
                [("query.to", "ends_before_starts")],
            ),
            (
                {"from": "2025-01-01T00:00:00Z", "to": "2026-01-03T00:00:00Z"},
                [("query.to", "out_of_range")],
            ),
        )
        for params, expected in cases:
            with self.subTest(params=params):
                response = self.get(params)
                self.assertEqual(
                    (response.status_code, response.json()["code"]), (422, "validation_error")
                )
                self.assertEqual(self.details(response), expected)
        self.assertEqual(self.capabilities.calls, [])

    def test_affiliation_down_or_slow(self) -> None:
        self.capabilities.unavailable = True
        down = self.get()
        slow = self.client_with(SlowAffiliation()).get(DASHBOARD, headers=self.auth)

        self.assertEqual((down.status_code, down.json()["code"]), (503, "upstream_unavailable"))
        self.assertEqual((slow.status_code, slow.json()["code"]), (504, "upstream_timeout"))

    def test_one_source_down_is_reported_and_both_down_is_503(self) -> None:
        self.finished("running", NOW - timedelta(days=1), 45)
        self.reader.metrics_down = True

        partial = self.get().json()
        self.reader.sessions_down = True
        both = self.get()

        self.assertEqual(partial["totals"]["sessions_count"], 1)
        self.assertEqual(
            partial["sources"],
            [
                {"source": "training_sessions", "status": "OK"},
                {"source": "training_metrics", "status": "UNAVAILABLE"},
            ],
        )
        self.assertEqual((both.status_code, both.json()["code"]), (503, "upstream_unavailable"))


if __name__ == "__main__":
    unittest.main()
