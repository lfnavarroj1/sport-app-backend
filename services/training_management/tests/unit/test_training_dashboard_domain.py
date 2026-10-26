"""Reglas del tablero de HU033 (ADR-043) sin infraestructura: periodo, semanas ISO en zonas con
desfase negativo y positivo, estado vacío, mezcla de fuentes, redondeo y último indicador."""

import unittest
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal

from training_management.domain.dashboard.model import (
    DashboardPeriod,
    InvalidDashboardPeriod,
    LatestMetric,
    SessionBucket,
    Source,
    build_dashboard,
    iso_week_start,
    minutes,
    resolve_period,
)

NOW = datetime(2026, 10, 24, 15, 0, tzinfo=UTC)
BOGOTA = timezone(timedelta(hours=-5))
TOKYO = timezone(timedelta(hours=9))
PERIOD = DashboardPeriod(
    start=datetime(2026, 10, 1, tzinfo=BOGOTA), end=datetime(2026, 11, 1, tzinfo=BOGOTA)
)


def bucket(
    sport: str = "running",
    week: date = date(2026, 10, 5),
    source: Source = Source.SPORTAPP,
    *,
    count: int = 1,
    seconds: int = 3600,
    distance: int = 0,
    with_distance: int = 0,
) -> SessionBucket:
    return SessionBucket(
        sport=sport,
        week_start=week,
        source=source,
        sessions_count=count,
        duration_seconds=seconds,
        distance_meters=distance,
        sessions_with_distance=with_distance,
    )


def metric(indicator: str, value: str, measured_at: datetime) -> LatestMetric:
    return LatestMetric(
        indicator=indicator,
        value=Decimal(value),
        unit="W",
        method_code="FTP_20_MIN_95",
        method_version=1,
        measured_at=measured_at,
        disclaimer_key="estimated_not_diagnostic",
    )


class DashboardPeriodTest(unittest.TestCase):
    def violation(self, raw_from: str | None, raw_to: str | None) -> tuple[str, str]:
        with self.assertRaises(InvalidDashboardPeriod) as raised:
            resolve_period(raw_from, raw_to, NOW)
        return raised.exception.field, raised.exception.issue

    def test_default_is_the_last_30_days_in_utc(self) -> None:
        period = resolve_period(None, None, NOW)

        self.assertEqual((period.start, period.end), (NOW - timedelta(days=30), NOW))
        self.assertEqual(period.offset, timedelta(0))

    def test_offsets_are_kept_and_the_from_offset_defines_the_zone(self) -> None:
        """CA3: `from`/`to` viajan con desfase y las semanas se calculan en esa zona."""
        period = resolve_period("2026-10-01T00:00:00-05:00", "2026-11-01T00:00:00Z", NOW)

        self.assertEqual(period.start, datetime(2026, 10, 1, tzinfo=BOGOTA))
        self.assertEqual(period.offset, timedelta(hours=-5))
        self.assertEqual(period.end, datetime(2026, 11, 1, tzinfo=UTC))

    def test_plus_decoded_as_space_is_read_as_a_positive_offset(self) -> None:
        period = resolve_period("2026-10-01T00:00:00 09:00", "2026-10-08T00:00:00 09:00", NOW)

        self.assertEqual(period.offset, timedelta(hours=9))

    def test_only_one_bound_is_required_on_the_missing_one(self) -> None:
        self.assertEqual(self.violation("2026-10-01T00:00:00Z", None), ("to", "required"))
        self.assertEqual(self.violation(None, "2026-10-01T00:00:00Z"), ("from", "required"))

    def test_instants_without_offset_or_malformed(self) -> None:
        for raw in (
            "2026-10-01",
            "2026-10-01T00:00:00",
            "2026-13-01T00:00:00Z",
            "ayer",
            "2026-10-01T00:00:00+5",
        ):
            with self.subTest(raw=raw):
                self.assertEqual(
                    self.violation(raw, "2026-11-01T00:00:00Z"), ("from", "invalid_datetime")
                )
        self.assertEqual(
            self.violation("2026-10-01T00:00:00Z", "2026-11-01"), ("to", "invalid_datetime")
        )

    def test_end_must_be_after_start(self) -> None:
        for raw_to in ("2026-10-01T00:00:00Z", "2026-09-30T00:00:00Z"):
            with self.subTest(to=raw_to):
                self.assertEqual(
                    self.violation("2026-10-01T00:00:00Z", raw_to), ("to", "ends_before_starts")
                )

    def test_at_most_366_days(self) -> None:
        """CA4: un periodo mayor de 366 días → `422`; exactamente 366 se acepta."""
        resolve_period("2025-10-01T00:00:00Z", "2026-10-02T00:00:00Z", NOW)

        self.assertEqual(
            self.violation("2025-10-01T00:00:00Z", "2026-10-02T00:00:01Z"), ("to", "out_of_range")
        )


class IsoWeekTest(unittest.TestCase):
    def test_week_starts_on_the_local_monday_with_a_negative_offset(self) -> None:
        """CA3 en `-05:00`: el domingo 23:30 local es aún la semana anterior aunque en UTC ya
        sea lunes."""
        sunday_night = datetime(2026, 10, 12, 4, 30, tzinfo=UTC)  # dom 11, 23:30 en Bogotá

        self.assertEqual(iso_week_start(sunday_night, timedelta(hours=-5)), date(2026, 10, 5))
        self.assertEqual(iso_week_start(sunday_night, timedelta(0)), date(2026, 10, 12))

    def test_week_starts_on_the_local_monday_with_a_positive_offset(self) -> None:
        """CA3 en `+09:00`: el lunes 01:00 local es la semana nueva aunque en UTC sea domingo."""
        monday_early = datetime(2026, 10, 11, 16, 0, tzinfo=UTC)  # lun 12, 01:00 en Tokio

        self.assertEqual(iso_week_start(monday_early, timedelta(hours=9)), date(2026, 10, 12))
        self.assertEqual(iso_week_start(monday_early, timedelta(0)), date(2026, 10, 5))

    def test_year_boundary_follows_iso_weeks(self) -> None:
        # 2027-01-01 es viernes: su semana ISO empieza el lunes 2026-12-28.
        self.assertEqual(
            iso_week_start(datetime(2027, 1, 1, 12, 0, tzinfo=TOKYO), timedelta(hours=9)),
            date(2026, 12, 28),
        )


class BuildDashboardTest(unittest.TestCase):
    def test_without_data_everything_is_zero_and_empty(self) -> None:
        """CA2: `has_data: false`, totales en cero, listas vacías y nada inventado."""
        dashboard = build_dashboard(PERIOD, [], [])

        self.assertFalse(dashboard.has_data)
        self.assertEqual(
            (
                dashboard.totals.sessions_count,
                dashboard.totals.total_minutes,
                dashboard.totals.average_minutes,
                dashboard.totals.distance,
            ),
            (0, 0, 0, None),
        )
        self.assertEqual(
            (dashboard.by_sport, dashboard.by_week, dashboard.latest_metrics), ((), (), ())
        )
        self.assertEqual(
            [(source, aggregate.sessions_count) for source, aggregate in dashboard.by_source],
            [(Source.SPORTAPP, 0), (Source.STRAVA, 0)],
        )

    def test_totals_sports_weeks_and_sources(self) -> None:
        """CA1: totales, por deporte (orden del catálogo), por semana y por fuente."""
        dashboard = build_dashboard(
            PERIOD,
            [
                bucket("swimming", date(2026, 10, 12), count=1, seconds=1800),
                bucket("running", date(2026, 10, 5), count=2, seconds=5400),
                bucket("running", date(2026, 10, 12), count=1, seconds=2700),
                bucket(
                    "cycling",
                    date(2026, 10, 12),
                    Source.STRAVA,
                    count=1,
                    seconds=3600,
                    distance=25000,
                    with_distance=1,
                ),
            ],
            [],
        )

        self.assertTrue(dashboard.has_data)
        self.assertEqual(
            (
                dashboard.totals.sessions_count,
                dashboard.totals.total_minutes,
                dashboard.totals.average_minutes,
                dashboard.totals.distance,
            ),
            (5, 225, 45, 25000),
        )
        self.assertEqual(
            [
                (sport, a.sessions_count, a.total_minutes, a.distance)
                for sport, a in dashboard.by_sport
            ],
            [("running", 3, 135, None), ("cycling", 1, 60, 25000), ("swimming", 1, 30, None)],
        )
        self.assertEqual(
            [(week, a.sessions_count, a.total_minutes) for week, a in dashboard.by_week],
            [(date(2026, 10, 5), 2, 90), (date(2026, 10, 12), 3, 135)],
        )
        self.assertEqual(
            [(source, a.sessions_count, a.total_minutes) for source, a in dashboard.by_source],
            [(Source.SPORTAPP, 4, 165), (Source.STRAVA, 1, 60)],
        )

    def test_distance_is_none_when_no_row_has_it(self) -> None:
        """Decisión del 2026-10-24: sin filas con distancia → `None`, no «0 m» inventado."""
        dashboard = build_dashboard(PERIOD, [bucket(count=3, seconds=9000)], [])

        self.assertIsNone(dashboard.totals.distance)
        self.assertIsNone(dashboard.by_sport[0][1].distance)

    def test_minutes_are_rounded_half_up_from_summed_seconds(self) -> None:
        self.assertEqual((minutes(89), minutes(90), minutes(149), minutes(150)), (1, 2, 2, 3))
        dashboard = build_dashboard(PERIOD, [bucket(count=3, seconds=3 * 1530)], [])

        # 4590 s → 76,5 min → 77; promedio 25,5 min → 26.
        self.assertEqual(
            (dashboard.totals.total_minutes, dashboard.totals.average_minutes), (77, 26)
        )

    def test_latest_metrics_alone_count_as_data(self) -> None:
        measured = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
        dashboard = build_dashboard(
            PERIOD,
            [],
            [
                metric("VO2MAX_ESTIMATED", "42.4", measured),
                metric("FTP_ESTIMATED", "238", measured),
            ],
        )

        self.assertTrue(dashboard.has_data)
        self.assertEqual(
            [item.indicator for item in dashboard.latest_metrics],
            ["FTP_ESTIMATED", "VO2MAX_ESTIMATED"],
        )
        self.assertEqual(dashboard.totals.sessions_count, 0)

    def test_source_availability_is_kept(self) -> None:
        dashboard = build_dashboard(PERIOD, [], [], metrics_available=False)

        self.assertEqual((dashboard.sessions_available, dashboard.metrics_available), (True, False))


if __name__ == "__main__":
    unittest.main()
