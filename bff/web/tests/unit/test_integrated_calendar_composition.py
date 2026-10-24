"""BFF web, HU018: reglas puras del calendario integrado (ADR-039 con ADR-031): periodo,
superposición, `occurs_on`, orden, límite por fuente, ventanas del catálogo y forma de cada
elemento."""

import unittest
import uuid
from datetime import UTC, datetime, timedelta, timezone
from itertools import pairwise

from bff_web.api.integrated_calendar_composition import (
    SOURCE_LIMIT,
    InvalidPeriod,
    Period,
    associated_service_item,
    availability_windows,
    capped,
    current_month,
    event_overlaps,
    interval_overlaps,
    normalize_period,
    ordered,
    own_event_item,
    own_service_item,
    published_event_item,
    registration_item,
    session_item,
)

BOGOTA = timezone(timedelta(hours=-5))
LOCATION = {"name": "Parque", "address": "Calle 1", "city": "Bogotá", "country_code": "CO"}


def event(starts_at: str, ends_at: str, **overrides: object) -> dict[str, object]:
    return {
        "event_id": str(uuid.uuid4()),
        "organizer_id": str(uuid.uuid4()),
        "name": "Carrera 10K",
        "discipline": "Running",
        "location": LOCATION,
        "starts_at": starts_at,
        "ends_at": ends_at,
        "time_zone": "America/Bogota",
        "status": "PUBLISHED",
        **overrides,
    }


def service(**overrides: object) -> dict[str, object]:
    return {
        "service_id": str(uuid.uuid4()),
        "provider_id": str(uuid.uuid4()),
        "name": "Valoración médica",
        "category": "SPORTS_MEDICINE",
        "modality": "IN_PERSON",
        "status": "DRAFT",
        **overrides,
    }


INTERVAL = {
    "starts_at": "2026-11-15T14:00:00Z",
    "ends_at": "2026-11-15T15:00:00Z",
    "time_zone": "America/Bogota",
    "capacity": 2,
}


class PeriodTest(unittest.TestCase):
    NOW = datetime(2026, 11, 20, 15, 0, tzinfo=UTC)

    def test_default_is_the_current_utc_month(self) -> None:
        period = normalize_period(None, None, self.NOW)
        self.assertEqual(period.period_from, datetime(2026, 11, 1, tzinfo=UTC))
        self.assertEqual(period.period_to, datetime(2026, 11, 30, 23, 59, 59, tzinfo=UTC))
        december = current_month(datetime(2026, 12, 31, 23, 0, tzinfo=UTC))
        self.assertEqual(december.period_to, datetime(2026, 12, 31, 23, 59, 59, tzinfo=UTC))

    def test_keeps_the_offset_and_derives_dates_in_it(self) -> None:
        period = normalize_period(
            "2026-11-01T00:00:00-05:00", "2026-11-30T23:59:59-05:00", self.NOW
        )
        self.assertEqual(period.period_from.utcoffset(), timedelta(hours=-5))
        self.assertEqual(
            (period.date_from.isoformat(), period.date_to.isoformat()), ("2026-11-01", "2026-11-30")
        )

    def test_same_instant_and_exactly_92_days_are_valid(self) -> None:
        normalize_period("2026-11-01T00:00:00Z", "2026-11-01T00:00:00Z", self.NOW)
        normalize_period("2026-11-01T00:00:00Z", "2027-02-01T00:00:00Z", self.NOW)

    def test_invalid_periods(self) -> None:
        """CA6: cada periodo inválido informa el extremo y la causa."""
        cases = [
            (("2026-11-01", None), ("to", "required")),
            ((None, "2026-11-01T00:00:00Z"), ("from", "required")),
            (("mañana", "2026-11-01T00:00:00Z"), ("from", "invalid_datetime")),
            (("2026-11-01T00:00:00Z", "2026-11-02T00:00:00"), ("to", "invalid_datetime")),
            (("2026-11-02T00:00:00Z", "2026-11-01T00:00:00Z"), ("to", "ends_before_starts")),
            (("2026-11-01T00:00:00Z", "2027-02-01T00:00:01Z"), ("to", "out_of_range")),
        ]
        for (raw_from, raw_to), expected in cases:
            with self.subTest(raw_from=raw_from, raw_to=raw_to):
                with self.assertRaises(InvalidPeriod) as raised:
                    normalize_period(raw_from, raw_to, self.NOW)
                self.assertEqual((raised.exception.field, raised.exception.issue), expected)
                self.assertEqual(raised.exception.code, "validation_error")


class OverlapTest(unittest.TestCase):
    PERIOD = Period(datetime(2026, 11, 1, tzinfo=UTC), datetime(2026, 11, 30, tzinfo=UTC))

    def test_events_overlap_inclusively(self) -> None:
        start, end = self.PERIOD.period_from, self.PERIOD.period_to
        hour = timedelta(hours=1)
        self.assertTrue(event_overlaps(self.PERIOD, start - hour, start))
        self.assertTrue(event_overlaps(self.PERIOD, end, end + hour))
        self.assertFalse(event_overlaps(self.PERIOD, start - 2 * hour, start - hour))
        self.assertFalse(event_overlaps(self.PERIOD, end + hour, end + 2 * hour))

    def test_service_intervals_end_exclusively(self) -> None:
        start, end = self.PERIOD.period_from, self.PERIOD.period_to
        hour = timedelta(hours=1)
        self.assertFalse(interval_overlaps(self.PERIOD, start - hour, start))
        self.assertTrue(interval_overlaps(self.PERIOD, start - hour, start + hour))
        self.assertTrue(interval_overlaps(self.PERIOD, end, end + hour))


class ItemsTest(unittest.TestCase):
    def test_own_event_keeps_status_and_hides_organizer(self) -> None:
        """CA1 y CA3: estado, referencia, instante en UTC y `occurs_on` en la zona del evento
        (21:00 en Bogotá es el día siguiente en UTC)."""
        source = event("2026-11-15T21:00:00-05:00", "2026-11-15T23:00:00-05:00", status="DRAFT")
        item = own_event_item(source)
        self.assertEqual(item["item_id"], f"OWN_EVENT:{source['event_id']}")
        self.assertEqual((item["source"], item["status"]), ("OWN_EVENT", "DRAFT"))
        self.assertEqual(item["occurs_on"], "2026-11-15")
        self.assertEqual(item["starts_at"], "2026-11-16T02:00:00Z")
        self.assertEqual(item["time_zone"], "America/Bogota")
        self.assertEqual(item["reference"], {"event_id": source["event_id"]})
        self.assertEqual(
            item["location"], {"name": "Parque", "city": "Bogotá", "country_code": "CO"}
        )
        self.assertNotIn(str(source["organizer_id"]), str(item))

    def test_published_event_and_unknown_zone(self) -> None:
        item = published_event_item(
            event("2026-11-16T02:00:00Z", "2026-11-16T03:00:00Z", time_zone="Mars/Olympus")
        )
        self.assertEqual((item["status"], item["occurs_on"]), ("PUBLISHED", "2026-11-16"))

    def test_registration(self) -> None:
        registration = {
            "registration_id": str(uuid.uuid4()),
            "event_id": str(uuid.uuid4()),
            "status": "REGISTERED",
            "event": {
                "name": "Gran fondo",
                "starts_at": "2026-11-20T12:00:00Z",
                "ends_at": "2026-11-20T18:00:00Z",
                "time_zone": "America/Bogota",
                "location": LOCATION,
            },
        }
        item = registration_item(registration)
        self.assertEqual((item["source"], item["status"]), ("EVENT_REGISTRATION", "REGISTERED"))
        self.assertEqual(
            item["reference"],
            {
                "event_id": registration["event_id"],
                "registration_id": registration["registration_id"],
            },
        )
        self.assertEqual(item["title"], "Gran fondo")

    def test_session_has_only_a_date(self) -> None:
        session = {
            "plan_id": str(uuid.uuid4()),
            "plan_title": "Base aeróbica",
            "session_id": str(uuid.uuid4()),
            "scheduled_date": "2026-11-16",
            "sport": "running",
            "duration_minutes": 45,
            "rpe": 4,
        }
        item = session_item(session)
        self.assertEqual((item["status"], item["occurs_on"]), ("ASSIGNED", "2026-11-16"))
        self.assertEqual(
            (item["starts_at"], item["ends_at"], item["time_zone"]), (None, None, None)
        )

    def test_service_intervals(self) -> None:
        own = service()
        item = own_service_item(own, INTERVAL)
        self.assertEqual(
            item["item_id"], f"OWN_SERVICE_AVAILABILITY:{own['service_id']}:2026-11-15T14:00:00Z"
        )
        self.assertEqual((item["status"], item["capacity"]), ("DRAFT", 2))
        self.assertEqual(item["reference"], {"service_id": own["service_id"]})
        self.assertNotIn(str(own["provider_id"]), str(item))
        associated = associated_service_item(service(status="PUBLISHED"), INTERVAL, "a-1")
        self.assertEqual(associated["status"], "AVAILABLE")
        self.assertEqual(associated["reference"]["association_id"], "a-1")
        self.assertEqual(associated["occurs_on"], "2026-11-15")


class OrderAndLimitTest(unittest.TestCase):
    def test_order_by_day_time_source_and_id(self) -> None:
        """Una sesión sin hora va primero en su día; luego por instante, fuente e id."""
        items = [
            {
                "occurs_on": "2026-11-16",
                "starts_at": "2026-11-16T12:00:00Z",
                "source": "B",
                "item_id": "B:2",
            },
            {
                "occurs_on": "2026-11-16",
                "starts_at": "2026-11-16T12:00:00Z",
                "source": "A",
                "item_id": "A:9",
            },
            {"occurs_on": "2026-11-16", "starts_at": None, "source": "Z", "item_id": "Z:1"},
            {
                "occurs_on": "2026-11-15",
                "starts_at": "2026-11-16T03:00:00Z",
                "source": "C",
                "item_id": "C:1",
            },
            {
                "occurs_on": "2026-11-16",
                "starts_at": "2026-11-16T08:00:00Z",
                "source": "C",
                "item_id": "C:3",
            },
        ]
        self.assertEqual(
            [item["item_id"] for item in ordered(items)], ["C:1", "Z:1", "C:3", "A:9", "B:2"]
        )

    def test_capped_keeps_the_first_200(self) -> None:
        items = [
            {
                "occurs_on": f"2026-11-{day:02d}",
                "starts_at": None,
                "source": "S",
                "item_id": f"S:{n}",
            }
            for day in range(1, 31)
            for n in range(10)
        ]
        kept, truncated = capped(items)
        self.assertTrue(truncated)
        self.assertEqual(len(kept), SOURCE_LIMIT)
        self.assertEqual(kept[-1]["occurs_on"], "2026-11-20")
        self.assertEqual(capped(items[:SOURCE_LIMIT])[1], False)


class AvailabilityWindowsTest(unittest.TestCase):
    def test_windows_of_31_days_cover_the_period(self) -> None:
        period = Period(datetime(2026, 11, 1, tzinfo=BOGOTA), datetime(2027, 2, 1, tzinfo=BOGOTA))
        windows = availability_windows(period, timedelta(days=31))
        self.assertEqual(len(windows), 3)
        self.assertEqual(windows[0][0], datetime(2026, 11, 1, 5, tzinfo=UTC))
        self.assertEqual(windows[-1][1], datetime(2027, 2, 1, 5, tzinfo=UTC))
        for (_, end), (start, _) in pairwise(windows):
            self.assertEqual(end, start)
        self.assertTrue(all(end - start <= timedelta(days=31) for start, end in windows))

    def test_an_instant_has_no_window(self) -> None:
        moment = datetime(2026, 11, 1, tzinfo=UTC)
        self.assertEqual(availability_windows(Period(moment, moment), timedelta(days=31)), [])


if __name__ == "__main__":
    unittest.main()
