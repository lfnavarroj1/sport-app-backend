"""HU033 (ADR-043) contra PostgreSQL real (requiere `TEST_DATABASE_URL`): recorrido sesión →
outbox → proyección → tablero, semanas ISO calculadas por SQL iguales a las del dominio en
zonas `-05:00` y `+09:00`, periodo `[from, to)`, último resultado por indicador, base de
lectura caída y rendimiento con 2.000 sesiones y 200 cálculos (p95 < 1 s)."""

import statistics
import time
import unittest
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import create_engine, insert, pool, text
from sqlalchemy.orm import sessionmaker
from support import (
    AlwaysCapable,
    TrainingDatabases,
    athlete_and_professional,
    finish_command,
    metric_command,
    start_command,
    training_databases,
)

from training_management.application.errors import DashboardSourceUnavailable
from training_management.application.messages import parse_training_event
from training_management.application.queries.training_dashboard import GetTrainingDashboard
from training_management.domain.dashboard.model import (
    DashboardPeriod,
    iso_week_start,
    resolve_period,
)
from training_management.infrastructure.clock import SystemClock
from training_management.infrastructure.persistence_read.dashboard import (
    SqlAlchemyDashboardReader,
)
from training_management.infrastructure.persistence_read.models import (
    TrainingMetricResultRow,
    TrainingSessionHistoryRow,
)
from training_management.infrastructure.persistence_write.outbox import SqlAlchemyOutboxStore
from training_management.workers.outbox_publisher import build_envelope

BOGOTA = timezone(timedelta(hours=-5))
TOKYO = timezone(timedelta(hours=9))
SPORTS = ("running", "cycling", "swimming", "strength", "other")
P95_LIMIT_SECONDS = 1.0
MEASURED_RUNS = 40


def history_row(
    athlete_id: UUID, ended_at: datetime, *, sport: str = "running", minutes: int = 45
) -> dict[str, Any]:
    return {
        "session_id": uuid4(),
        "athlete_id": athlete_id,
        "plan_id": None,
        "sport": sport,
        "started_at": ended_at - timedelta(minutes=minutes),
        "ended_at": ended_at,
        "duration_seconds": minutes * 60,
        "version": 2,
        "projected_at": ended_at,
    }


def metric_row(
    athlete_id: UUID,
    measured_at: datetime,
    *,
    code: str = "FTP_ESTIMATED",
    value: str = "238",
    calculation_id: UUID | None = None,
) -> dict[str, Any]:
    vo2 = code == "VO2MAX_ESTIMATED"
    return {
        "calculation_id": calculation_id or uuid4(),
        "athlete_id": athlete_id,
        "metric_code": code,
        "method_code": "COOPER_12_MIN_V1" if vo2 else "FTP_20_MIN_95_V1",
        "method_version": 1,
        "measured_at": measured_at,
        "calculated_at": measured_at,
        "inputs": [],
        "result_value": Decimal(value),
        "result_unit": "mL/kg/min" if vo2 else "W",
        "result_scale": 1 if vo2 else 0,
        "status": "CONFIRMED",
        "disclaimer_key": "estimated_not_diagnostic",
        "version": 1,
        "projected_at": measured_at,
    }


class DashboardPersistenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.db: TrainingDatabases = self.enterContext(training_databases("hu033"))
        self.athlete, _ = athlete_and_professional()
        self.reader = SqlAlchemyDashboardReader(self.db.reads)

    def dashboard(self) -> GetTrainingDashboard:
        return GetTrainingDashboard(
            reader=self.reader, capabilities=AlwaysCapable(), clock=SystemClock()
        )

    def seed(self, rows: list[dict[str, Any]], model: Any = TrainingSessionHistoryRow) -> None:
        with self.db.reads() as session, session.begin():
            session.execute(insert(model), rows)

    def project_outbox(self) -> None:
        store = SqlAlchemyOutboxStore(self.db.writes)
        project = self.db.project()
        for event in store.claim_batch("w-test", 100, 30):
            project(parse_training_event(build_envelope(event)))
            store.mark_published(event.event_id, "w-test")

    def test_finished_sessions_and_a_metric_reach_the_dashboard(self) -> None:
        """CA1 con el recorrido real: dos sesiones de HU032 y una FTP de HU034 proyectadas."""
        empty = self.dashboard()(self.athlete, None, None)
        for sport in ("running", "cycling"):
            session_id = self.db.start_session()(
                self.athlete, start_command(sport=sport)
            ).session.session_id
            self.db.finish_session()(self.athlete, finish_command(session_id))
        self.db.calculate_metric()(self.athlete, metric_command(watts=250))

        self.project_outbox()
        dashboard = self.dashboard()(self.athlete, None, None)

        self.assertFalse(empty.has_data)
        self.assertTrue(dashboard.has_data)
        self.assertEqual(dashboard.totals.sessions_count, 2)
        self.assertEqual(dashboard.totals.total_minutes, 70)  # dos sesiones de 35 min
        self.assertEqual([sport for sport, _ in dashboard.by_sport], ["running", "cycling"])
        [ftp] = dashboard.latest_metrics
        self.assertEqual((ftp.indicator, ftp.value, ftp.unit), ("FTP_ESTIMATED", Decimal(238), "W"))

    def test_sql_weeks_match_the_domain_with_negative_and_positive_offsets(self) -> None:
        """CA3: la semana ISO de SQL coincide con `iso_week_start` en `-05:00`, `+09:00` y UTC,
        también en los bordes (domingo noche y lunes temprano locales)."""
        instants = [
            datetime(2026, 10, 12, 4, 30, tzinfo=UTC),  # dom 11, 23:30 en Bogotá
            datetime(2026, 10, 11, 16, 0, tzinfo=UTC),  # lun 12, 01:00 en Tokio
            datetime(2026, 10, 12, 0, 0, tzinfo=UTC),  # lunes 00:00 UTC
            datetime(2026, 12, 31, 23, 0, tzinfo=UTC),  # cambio de año
        ]
        self.seed([history_row(self.athlete.user_id, instant) for instant in instants])
        for zone in (BOGOTA, TOKYO, UTC):
            with self.subTest(zone=str(zone)):
                period = DashboardPeriod(
                    start=datetime(2026, 9, 1, tzinfo=zone), end=datetime(2027, 1, 31, tzinfo=zone)
                )
                buckets = self.reader.session_buckets(self.athlete.user_id, period)
                expected: dict[date, int] = {}
                for instant in instants:
                    week = iso_week_start(instant, period.offset)
                    expected[week] = expected.get(week, 0) + 1
                self.assertEqual(
                    {item.week_start: item.sessions_count for item in buckets}, expected
                )

    def test_strava_rows_are_grouped_by_source_with_their_distance(self) -> None:
        """HU031 (ADR-042 §12): `source` y `distance_meters` reales de la tabla; las filas sin
        `source` explícito toman `SPORTAPP` por defecto y no cuentan para la distancia."""
        athlete = self.athlete.user_id
        ended = datetime(2026, 10, 7, 12, tzinfo=UTC)
        strava = [
            {
                **history_row(athlete, ended, minutes=60),
                "source": "STRAVA",
                "distance_meters": 10234,
            },
            {
                **history_row(athlete, ended, sport="cycling", minutes=90),
                "source": "STRAVA",
                "distance_meters": 40500,
            },
            {
                **history_row(athlete, ended, minutes=20),
                "source": "STRAVA",
                "distance_meters": None,
            },
        ]
        self.seed([history_row(athlete, ended, minutes=30)])  # SportApp, sin distancia
        self.seed(strava)
        period = DashboardPeriod(
            datetime(2026, 10, 1, tzinfo=UTC), datetime(2026, 11, 1, tzinfo=UTC)
        )

        buckets = self.reader.session_buckets(athlete, period)
        dashboard = self.dashboard()(self.athlete, "2026-10-01T00:00:00Z", "2026-11-01T00:00:00Z")

        by_key = {
            (item.sport, item.source.value): (
                item.sessions_count,
                item.distance_meters,
                item.sessions_with_distance,
            )
            for item in buckets
        }
        self.assertEqual(
            by_key,
            {
                ("running", "SPORTAPP"): (1, 0, 0),
                ("running", "STRAVA"): (2, 10234, 1),
                ("cycling", "STRAVA"): (1, 40500, 1),
            },
        )
        self.assertEqual(dashboard.totals.distance, 50734)
        self.assertEqual(
            [(source.value, a.sessions_count) for source, a in dashboard.by_source],
            [("SPORTAPP", 1), ("STRAVA", 3)],
        )

    def test_period_is_half_open_and_only_the_caller_is_aggregated(self) -> None:
        start, end = datetime(2026, 10, 1, tzinfo=BOGOTA), datetime(2026, 10, 8, tzinfo=BOGOTA)
        other = uuid4()
        self.seed(
            [
                history_row(self.athlete.user_id, start),  # dentro (inclusive)
                history_row(self.athlete.user_id, end - timedelta(seconds=1)),  # dentro
                history_row(self.athlete.user_id, end),  # fuera (exclusivo)
                history_row(self.athlete.user_id, start - timedelta(seconds=1)),  # fuera
                history_row(other, start + timedelta(days=1)),  # otro deportista
            ]
        )

        buckets = self.reader.session_buckets(self.athlete.user_id, DashboardPeriod(start, end))

        self.assertEqual(sum(item.sessions_count for item in buckets), 2)

    def test_latest_result_per_indicator_within_the_period(self) -> None:
        period = DashboardPeriod(
            datetime(2026, 10, 1, tzinfo=UTC), datetime(2026, 11, 1, tzinfo=UTC)
        )
        mid = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
        tie_low, tie_high = sorted([uuid4(), uuid4()])
        athlete = self.athlete.user_id
        self.seed(
            [
                metric_row(athlete, mid - timedelta(days=5), value="230"),
                metric_row(athlete, mid, value="236", calculation_id=tie_low),
                metric_row(athlete, mid, value="238", calculation_id=tie_high),
                metric_row(athlete, datetime(2026, 11, 1, tzinfo=UTC), value="300"),  # fuera
                metric_row(athlete, mid, code="VO2MAX_ESTIMATED", value="42.4"),
                metric_row(uuid4(), mid + timedelta(days=1), value="999"),  # otro deportista
            ],
            TrainingMetricResultRow,
        )

        metrics = {item.indicator: item for item in self.reader.latest_metrics(athlete, period)}

        self.assertEqual(set(metrics), {"FTP_ESTIMATED", "VO2MAX_ESTIMATED"})
        self.assertEqual(metrics["FTP_ESTIMATED"].value, Decimal("238"))
        self.assertEqual(str(metrics["VO2MAX_ESTIMATED"].value), "42.4")

    def test_read_database_down_is_a_source_unavailable(self) -> None:
        engine = create_engine(
            "postgresql+psycopg://postgres@127.0.0.1:1/sportapp",
            poolclass=pool.NullPool,
            connect_args={"connect_timeout": 1},
        )
        self.addCleanup(engine.dispose)
        reader = SqlAlchemyDashboardReader(sessionmaker(engine))
        period = resolve_period(None, None, datetime.now(UTC))

        with self.assertRaises(DashboardSourceUnavailable):
            reader.session_buckets(self.athlete.user_id, period)
        with self.assertRaises(DashboardSourceUnavailable):
            reader.latest_metrics(self.athlete.user_id, period)

    def test_p95_below_one_second_with_2000_sessions_and_200_calculations(self) -> None:
        """CA4 y ADR-043 §6: 2.000 sesiones y 200 cálculos del mismo deportista en 366 días,
        más carga de otros deportistas; p95 del caso de uso completo < 1 s."""
        athlete = self.athlete.user_id
        end = datetime(2026, 10, 24, tzinfo=BOGOTA)
        sessions = [
            history_row(
                athlete,
                end - timedelta(minutes=263 * index + 1),
                sport=SPORTS[index % len(SPORTS)],
                minutes=20 + index % 70,
            )
            for index in range(2000)
        ]
        noise = [history_row(uuid4(), end - timedelta(hours=index)) for index in range(2000)]
        self.seed(sessions + noise)
        self.seed(
            [
                metric_row(
                    athlete,
                    end - timedelta(hours=43 * index + 1),
                    code="FTP_ESTIMATED" if index % 2 else "VO2MAX_ESTIMATED",
                    value="238" if index % 2 else "42.4",
                )
                for index in range(200)
            ],
            TrainingMetricResultRow,
        )
        with self.db.reads() as session, session.begin():
            session.execute(text("ANALYZE training_session_history, training_metric_result"))
        get = self.dashboard()
        raw_from, raw_to = (end - timedelta(days=366)).isoformat(), end.isoformat()

        get(self.athlete, raw_from, raw_to)  # calentamiento (conexiones y planes)
        timings = []
        for _ in range(MEASURED_RUNS):
            started = time.perf_counter()
            dashboard = get(self.athlete, raw_from, raw_to)
            timings.append(time.perf_counter() - started)
        p95 = statistics.quantiles(timings, n=20)[-1]

        self.assertEqual(dashboard.totals.sessions_count, 2000)
        self.assertEqual(len(dashboard.latest_metrics), 2)
        self.assertLess(p95, P95_LIMIT_SECONDS, f"p95 = {p95 * 1000:.1f} ms")


if __name__ == "__main__":
    unittest.main()
