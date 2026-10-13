"""HU021 CA3: forma del JSON y reglas del evento (ADR-028), sin infraestructura."""

import unittest
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from fakes import NOW, event_body, location

from events_management.api.payload import parse_event
from events_management.domain.sport_events.countries import COUNTRY_CODES
from events_management.domain.sport_events.model import (
    EventStatus,
    SportEvent,
    validate_event,
)
from events_management.domain.validation import InvalidSportEvent


def issues(body: object, now: datetime = NOW) -> set[tuple[str | None, str]]:
    try:
        validate_event(parse_event(body), now)
    except InvalidSportEvent as error:
        return {(item.field, item.issue.value) for item in error.violations}
    return set()


class PayloadShapeTest(unittest.TestCase):
    def test_server_owned_and_unknown_fields_are_not_allowed(self) -> None:
        body = event_body(
            organizer_id=str(uuid4()),
            event_id=str(uuid4()),
            ends_at="2026-11-15T10:00:00-05:00",
            status="PUBLISHED",
            version=2,
            created_at="2026-10-12T21:00:00Z",
            price="10000",
            location=location(latitude=4.65),
        )

        self.assertEqual(
            issues(body),
            {
                ("organizer_id", "field_not_allowed"),
                ("event_id", "field_not_allowed"),
                ("ends_at", "field_not_allowed"),
                ("status", "field_not_allowed"),
                ("version", "field_not_allowed"),
                ("created_at", "field_not_allowed"),
                ("price", "field_not_allowed"),
                ("location.latitude", "field_not_allowed"),
            },
        )

    def test_required_fields(self) -> None:
        self.assertEqual(
            issues({}),
            {
                (name, "required")
                for name in (
                    "name",
                    "description",
                    "discipline",
                    "location",
                    "starts_at",
                    "time_zone",
                    "duration_minutes",
                    "capacity",
                )
            },
        )
        self.assertEqual(
            issues(event_body(location={})),
            {
                ("location.name", "required"),
                ("location.address", "required"),
                ("location.city", "required"),
                ("location.country_code", "required"),
            },
        )
        self.assertEqual(issues(None), {(None, "required")})
        self.assertEqual(issues([]), {(None, "unknown_value")})

    def test_json_types(self) -> None:
        body = event_body(
            name=12, location="Bogotá", duration_minutes="180", capacity=True, starts_at=1
        )

        self.assertEqual(
            issues(body),
            {
                ("name", "unknown_value"),
                ("location", "unknown_value"),
                ("duration_minutes", "unknown_value"),
                ("capacity", "unknown_value"),
                ("starts_at", "unknown_value"),
            },
        )


class EventRulesTest(unittest.TestCase):
    def test_adr_example_is_normalized_to_utc(self) -> None:
        details = validate_event(parse_event(event_body(name="  Carrera 10K Bogotá  ")), NOW)

        self.assertEqual(details.name, "Carrera 10K Bogotá")
        self.assertEqual(details.starts_at, datetime(2026, 11, 15, 12, 0, tzinfo=UTC))
        self.assertEqual(details.ends_at, datetime(2026, 11, 15, 15, 0, tzinfo=UTC))
        self.assertEqual(details.time_zone, "America/Bogota")

    def test_text_lengths_after_trimming(self) -> None:
        body = event_body(
            name="  ab ",
            description="x" * 19,
            discipline="R",
            location=location(name="P" * 121, address="Cl 1", city="B", country_code="CO"),
        )

        self.assertEqual(
            issues(body),
            {
                ("name", "invalid_length"),
                ("description", "invalid_length"),
                ("discipline", "invalid_length"),
                ("location.name", "invalid_length"),
                ("location.address", "invalid_length"),
                ("location.city", "invalid_length"),
            },
        )
        self.assertEqual(issues(event_body(name="   ")), {("name", "invalid_length")})

    def test_text_limits_are_inclusive(self) -> None:
        body = event_body(
            name="a" * 120,
            description="d" * 2000,
            discipline="a" * 80,
            location=location(name="abc", address="Cl 12", city="Ab"),
        )

        self.assertEqual(issues(body), set())

    def test_country_code_iso_3166_alpha_2_uppercase(self) -> None:
        self.assertEqual(len(COUNTRY_CODES), 249)
        for code in ("co", "XX", "COL", "UK", ""):
            with self.subTest(code=code):
                self.assertEqual(
                    issues(event_body(location=location(country_code=code))),
                    {("location.country_code", "unknown_value")},
                )

    def test_duration_and_capacity_ranges(self) -> None:
        for field, value, valid in (
            ("duration_minutes", 14, False),
            ("duration_minutes", 15, True),
            ("duration_minutes", 1440, True),
            ("duration_minutes", 1441, False),
            ("capacity", 0, False),
            ("capacity", 1, True),
            ("capacity", 100_000, True),
            ("capacity", 100_001, False),
        ):
            with self.subTest(field=field, value=value):
                expected = set() if valid else {(field, "out_of_range")}
                self.assertEqual(issues(event_body(**{field: value})), expected)

    def test_start_must_be_future_with_explicit_offset(self) -> None:
        past = (NOW - timedelta(minutes=1)).isoformat()
        cases: dict[str, set[tuple[str | None, str]]] = {
            past: {("starts_at", "not_in_future")},
            NOW.isoformat(): {("starts_at", "not_in_future")},
            "2026-11-15T07:00:00": {("starts_at", "unknown_value")},
            "15/11/2026 07:00": {("starts_at", "unknown_value")},
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(issues(event_body(starts_at=value, time_zone="UTC")), expected)

    def test_time_zone_unknown_and_offset_mismatch(self) -> None:
        unknown = issues(event_body(time_zone="America/Bogotá"))
        traversal = issues(event_body(time_zone="../etc/passwd"))
        mismatch = issues(event_body(time_zone="Europe/Madrid"))

        self.assertEqual(unknown, {("time_zone", "time_zone_unknown")})
        self.assertEqual(traversal, {("time_zone", "time_zone_unknown")})
        self.assertEqual(mismatch, {("time_zone", "time_zone_offset_mismatch")})

    def test_offset_follows_daylight_saving_of_the_zone(self) -> None:
        """Nueva York: UTC-04:00 en verano y UTC-05:00 en invierno."""
        summer: dict[str, Any] = {"time_zone": "America/New_York"}
        self.assertEqual(issues(event_body(starts_at="2027-07-01T09:00:00-04:00", **summer)), set())
        self.assertEqual(
            issues(event_body(starts_at="2027-07-01T09:00:00-05:00", **summer)),
            {("time_zone", "time_zone_offset_mismatch")},
        )
        self.assertEqual(issues(event_body(starts_at="2027-01-15T09:00:00-05:00", **summer)), set())

    def test_reports_every_violation_at_once(self) -> None:
        body = event_body(name="x", capacity=0, organizer_id=str(uuid4()))

        self.assertEqual(
            issues(body),
            {
                ("name", "invalid_length"),
                ("capacity", "out_of_range"),
                ("organizer_id", "field_not_allowed"),
            },
        )

    def test_new_event_is_a_version_one_draft(self) -> None:
        details = validate_event(parse_event(event_body()), NOW)
        organizer = uuid4()

        event = SportEvent.draft(event_id=uuid4(), organizer_id=organizer, details=details, now=NOW)

        self.assertEqual((event.status, event.version), (EventStatus.DRAFT, 1))
        self.assertEqual(event.organizer_id, organizer)
        self.assertEqual(event.created_at, event.updated_at)


if __name__ == "__main__":
    unittest.main()
