"""Matriz de reglas de ADR-027 para la oferta (HU015 CA3, CA6, CA7), sin infraestructura."""

import unittest
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from fakes import NOW, interval, service_body

from complementary_services.api.payload import parse_offer
from complementary_services.domain.catalog.offer import validate_offer
from complementary_services.domain.validation import InvalidServiceOffer, Issue, Violation


def problems(body: dict[str, Any]) -> set[tuple[str | None, str]]:
    with unittest.TestCase().assertRaises(InvalidServiceOffer) as raised:
        validate_offer(parse_offer(body), NOW)
    return {(item.field, item.issue.value) for item in raised.exception.violations}


def price(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {"amount": "120000.00", "currency": "COP", "billing_unit": "SESSION"}
    values.update(overrides)
    return values


class OfferRulesTest(unittest.TestCase):
    def test_valid_offer_is_normalized(self) -> None:
        offer = validate_offer(
            parse_offer(
                service_body(
                    name="  Valoración deportiva inicial  ",
                    price=price(amount="120000.5"),
                )
            ),
            NOW,
        )

        self.assertEqual(offer.name, "Valoración deportiva inicial")
        self.assertEqual(offer.price.amount, Decimal("120000.50"))
        [only] = offer.availability
        self.assertEqual(only.starts_at, datetime(2026, 10, 20, 14, 0, tzinfo=UTC))
        self.assertEqual(only.time_zone, "America/Bogota")

    def test_amount_limits_and_precision(self) -> None:
        """CA3/CA7: 0.00 a 999999999.99, máximo dos decimales, cadena decimal."""
        for amount in ("0", "0.00", "999999999.99"):
            with self.subTest(amount=amount):
                validate_offer(parse_offer(service_body(price=price(amount=amount))), NOW)
        cases = {
            "-0.01": "out_of_range",
            "1000000000.00": "out_of_range",
            "10.123": "too_many_decimals",
            "doce": "unknown_value",
            "NaN": "unknown_value",
        }
        for amount, issue in cases.items():
            with self.subTest(amount=amount):
                self.assertEqual(
                    problems(service_body(price=price(amount=amount))), {("price.amount", issue)}
                )

    def test_currency_and_catalogs(self) -> None:
        """CA3: solo COP y catálogos cerrados."""
        self.assertEqual(
            problems(service_body(price=price(currency="USD"))),
            {("price.currency", "unsupported_currency")},
        )
        self.assertEqual(
            problems(
                service_body(
                    category="PHYSIOTHERAPY",
                    modality="REMOTE",
                    price=price(billing_unit="MONTH"),
                )
            ),
            {
                ("category", "unknown_value"),
                ("modality", "unknown_value"),
                ("price.billing_unit", "unknown_value"),
            },
        )

    def test_text_lengths_after_trimming(self) -> None:
        """CA3: nombre 3-120 y descripción 20-2.000 tras recortar espacios exteriores."""
        self.assertEqual(
            problems(service_body(name="  ab  ", description=" " + "x" * 19 + " ")),
            {("name", "invalid_length"), ("description", "invalid_length")},
        )
        validate_offer(parse_offer(service_body(name="abc", description="x" * 2000)), NOW)
        self.assertEqual(problems(service_body(name="x" * 121)), {("name", "invalid_length")})

    def test_availability_is_optional(self) -> None:
        offer = validate_offer(parse_offer(service_body(availability=None)), NOW)

        self.assertEqual(offer.availability, ())


class IntervalRulesTest(unittest.TestCase):
    """CA6 y la evidencia de intervalos de ADR-027."""

    def check(self, *intervals: dict[str, Any]) -> set[tuple[str | None, str]]:
        return problems(service_body(availability=list(intervals)))

    def test_future_duration_and_horizon(self) -> None:
        self.assertEqual(
            self.check(
                interval(starts_at="2026-10-12T09:00:00-05:00", ends_at="2026-10-12T10:00:00-05:00")
            ),
            {("availability[0].starts_at", "not_in_future")},
        )
        self.assertEqual(
            self.check(interval(ends_at="2026-10-20T09:00:00-05:00")),
            {("availability[0].ends_at", "ends_before_starts")},
        )
        self.assertEqual(
            self.check(interval(ends_at="2026-10-21T09:00:01-05:00")),
            {("availability[0].ends_at", "out_of_range")},
        )
        # Exactamente 24 horas es válido; el fin a 365 días también.
        self.check_valid(interval(ends_at="2026-10-21T09:00:00-05:00"))
        self.check_valid(
            interval(starts_at="2027-10-12T09:00:00-05:00", ends_at="2027-10-12T10:00:00-05:00")
        )
        self.assertEqual(
            self.check(
                interval(starts_at="2027-10-12T09:30:00-05:00", ends_at="2027-10-12T10:30:00-05:00")
            ),
            {("availability[0].ends_at", "out_of_range")},
        )

    def check_valid(self, *intervals: dict[str, Any]) -> None:
        validate_offer(parse_offer(service_body(availability=list(intervals))), NOW)

    def test_time_zone_and_offset(self) -> None:
        self.assertEqual(
            self.check(interval(time_zone="Mars/Olympus")),
            {("availability[0].time_zone", "time_zone_unknown")},
        )
        self.assertEqual(
            self.check(interval(time_zone="Europe/Madrid")),
            {("availability[0].time_zone", "time_zone_offset_mismatch")},
        )
        # Madrid cambia de +02:00 a +01:00 el 25 de octubre de 2026: el desfase debe ser el
        # vigente en cada instante.
        self.check_valid(
            interval(
                starts_at="2026-10-24T10:00:00+02:00",
                ends_at="2026-10-24T11:00:00+02:00",
                time_zone="Europe/Madrid",
            ),
            interval(
                starts_at="2026-10-26T10:00:00+01:00",
                ends_at="2026-10-26T11:00:00+01:00",
                time_zone="Europe/Madrid",
            ),
        )
        self.assertEqual(
            self.check(interval(starts_at="2026-10-20T09:00:00", ends_at="2026-10-20T10:00:00")),
            {
                ("availability[0].starts_at", "unknown_value"),
                ("availability[0].ends_at", "unknown_value"),
            },
        )

    def test_capacity(self) -> None:
        for capacity in (0, 101):
            with self.subTest(capacity=capacity):
                self.assertEqual(
                    self.check(interval(capacity=capacity)),
                    {("availability[0].capacity", "out_of_range")},
                )
        self.check_valid(interval(capacity=100))

    def test_contiguous_allowed_and_overlap_rejected(self) -> None:
        self.check_valid(
            interval(),
            interval(starts_at="2026-10-20T10:00:00-05:00", ends_at="2026-10-20T11:00:00-05:00"),
        )
        long = interval(starts_at="2026-10-21T08:00:00-05:00", ends_at="2026-10-21T18:00:00-05:00")
        inside = interval(
            starts_at="2026-10-21T12:00:00-05:00", ends_at="2026-10-21T13:00:00-05:00"
        )
        late = interval(starts_at="2026-10-21T16:00:00-05:00", ends_at="2026-10-21T17:00:00-05:00")

        self.assertEqual(
            self.check(long, inside, late),
            {
                ("availability[1]", "overlapping_interval"),
                ("availability[2]", "overlapping_interval"),
            },
        )

    def test_overlap_is_detected_across_time_zones(self) -> None:
        """Se compara en UTC: 09:00 Bogotá es 16:00 Madrid (+02:00)."""
        madrid = interval(
            starts_at="2026-10-20T16:30:00+02:00",
            ends_at="2026-10-20T17:30:00+02:00",
            time_zone="Europe/Madrid",
        )

        self.assertEqual(
            self.check(interval(), madrid), {("availability[1]", "overlapping_interval")}
        )

    def test_at_most_100_intervals(self) -> None:
        many = [
            interval(
                starts_at=f"2026-11-{1 + i // 10:02d}T{8 + i % 10:02d}:00:00-05:00",
                ends_at=f"2026-11-{1 + i // 10:02d}T{8 + i % 10:02d}:30:00-05:00",
            )
            for i in range(100)
        ]
        self.check_valid(*many)

        self.assertEqual(
            self.check(*many, interval(starts_at="2026-12-01T08:00:00-05:00")),
            {("availability", "too_many_intervals")},
        )


class ViolationShapeTest(unittest.TestCase):
    def test_all_issues_belong_to_the_adr_catalog(self) -> None:
        self.assertEqual(
            {issue.value for issue in Issue},
            {
                "required",
                "field_not_allowed",
                "unsupported_currency",
                "out_of_range",
                "too_many_decimals",
                "invalid_length",
                "unknown_value",
                "not_in_future",
                "ends_before_starts",
                "overlapping_interval",
                "time_zone_unknown",
                "time_zone_offset_mismatch",
                "too_many_intervals",
            },
        )
        self.assertEqual(Violation("name", Issue.REQUIRED).issue, "required")


if __name__ == "__main__":
    unittest.main()
