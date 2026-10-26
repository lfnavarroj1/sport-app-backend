"""BFF móvil, HU029 (ADR-041): reglas puras de las sugerencias de acompañamiento.

Solapamiento intervalo-día del evento en la zona del evento, motivos por categoría,
exclusión de `SPORTS_MEDICINE` y orden determinista.
"""

import unittest
import uuid
from datetime import UTC, datetime
from typing import Any

from bff_mobile.api.service_suggestion_rules import (
    EVENT_LOGISTICS,
    UPCOMING_AVAILABILITY,
    Interval,
    event_days,
    instant,
    overlaps,
    suggest,
)


def interval(starts_at: str, ends_at: str, time_zone: str = "America/Bogota") -> dict[str, Any]:
    return {"starts_at": starts_at, "ends_at": ends_at, "time_zone": time_zone, "capacity": 2}


def service(category: str, *intervals: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "service_id": str(uuid.uuid4()),
        "provider_id": str(uuid.uuid4()),
        "name": f"Servicio {category}",
        "description": "Servicio sintético para las reglas de sugerencia.",
        "category": category,
        "modality": "IN_PERSON",
        "price": {"amount": "50000.00", "currency": "COP", "billing_unit": "SERVICE"},
        "availability": list(intervals),
        "available": True,
    }
    values.update(overrides)
    return values


# Carrera el sábado 2026-11-07 de 07:00 a 11:00 en Bogotá (UTC-5): 12:00Z a 16:00Z.
RACE = event_days(
    instant("2026-11-07T12:00:00Z"), instant("2026-11-07T16:00:00Z"), "America/Bogota"
)


class EventDaysTest(unittest.TestCase):
    def test_the_day_is_taken_in_the_time_zone_of_the_event(self) -> None:
        self.assertEqual(RACE.starts_at, datetime(2026, 11, 7, 5, 0, tzinfo=UTC))
        self.assertEqual(RACE.ends_at, datetime(2026, 11, 8, 5, 0, tzinfo=UTC))

    def test_a_late_evening_event_belongs_to_its_local_day_not_the_utc_one(self) -> None:
        # 2026-11-07 21:00 en Bogotá ya es 2026-11-08 en UTC.
        days = event_days(
            instant("2026-11-08T02:00:00Z"), instant("2026-11-08T04:00:00Z"), "America/Bogota"
        )

        self.assertEqual(days, RACE)

    def test_a_multi_day_event_covers_every_local_day(self) -> None:
        days = event_days(
            instant("2026-11-07T12:00:00Z"), instant("2026-11-09T05:00:00Z"), "America/Bogota"
        )

        # Termina exactamente a la medianoche local del 9: no ocupa ese día.
        self.assertEqual(days.starts_at, datetime(2026, 11, 7, 5, 0, tzinfo=UTC))
        self.assertEqual(days.ends_at, datetime(2026, 11, 9, 5, 0, tzinfo=UTC))

    def test_overlap_is_half_open(self) -> None:
        def window(starts_at: str, ends_at: str) -> Interval:
            return Interval(instant(starts_at), instant(ends_at), "UTC")

        self.assertTrue(overlaps(window("2026-11-07T04:00:00Z", "2026-11-07T05:30:00Z"), RACE))
        self.assertTrue(overlaps(window("2026-11-08T04:00:00Z", "2026-11-08T06:00:00Z"), RACE))
        self.assertFalse(overlaps(window("2026-11-07T04:00:00Z", "2026-11-07T05:00:00Z"), RACE))
        self.assertFalse(overlaps(window("2026-11-08T05:00:00Z", "2026-11-08T06:00:00Z"), RACE))


class SuggestTest(unittest.TestCase):
    def test_logistics_on_the_event_day_first_then_coaching_by_start(self) -> None:
        """CA1: las tres categorías; primero las que coinciden con el día del evento."""
        coaching_late = service(
            "COACHING", interval("2026-11-20T13:00:00Z", "2026-11-20T14:00:00Z")
        )
        coaching_soon = service(
            "COACHING", interval("2026-11-02T13:00:00Z", "2026-11-02T14:00:00Z")
        )
        transport = service(
            "TRANSPORT",
            interval("2026-11-01T10:00:00Z", "2026-11-01T11:00:00Z"),
            interval("2026-11-07T10:00:00Z", "2026-11-07T12:00:00Z"),
        )
        company = service("ACCOMPANIMENT", interval("2026-11-07T22:00:00Z", "2026-11-08T01:00:00Z"))

        result = suggest([coaching_late, company, coaching_soon, transport], [RACE])

        self.assertEqual(
            [(item.service["service_id"], item.reason) for item in result],
            [
                (transport["service_id"], EVENT_LOGISTICS),
                (company["service_id"], EVENT_LOGISTICS),
                (coaching_soon["service_id"], UPCOMING_AVAILABILITY),
                (coaching_late["service_id"], UPCOMING_AVAILABILITY),
            ],
        )
        # El intervalo informado es el que coincide con el evento, no el primero del servicio.
        self.assertEqual(result[0].interval.starts_at, instant("2026-11-07T10:00:00Z"))

    def test_logistics_without_a_matching_event_day_is_never_suggested(self) -> None:
        """CA2: sin coincidencia válida no hay servicios arbitrarios."""
        transport = service("TRANSPORT", interval("2026-11-09T10:00:00Z", "2026-11-09T12:00:00Z"))
        company = service("ACCOMPANIMENT", interval("2026-11-07T10:00:00Z", "2026-11-07T12:00:00Z"))

        self.assertEqual(suggest([transport], [RACE]), [])
        self.assertEqual(suggest([transport, company], []), [])

    def test_sports_medicine_and_other_categories_are_excluded(self) -> None:
        on_the_day = interval("2026-11-07T10:00:00Z", "2026-11-07T12:00:00Z")

        result = suggest(
            [service("SPORTS_MEDICINE", on_the_day), service("NUTRITION", on_the_day)], [RACE]
        )

        self.assertEqual(result, [])

    def test_services_without_available_intervals_are_not_suggested(self) -> None:
        """CA4: sin intervalos disponibles deja de sugerirse."""
        self.assertEqual(suggest([service("COACHING")], [RACE]), [])
        self.assertEqual(
            suggest(
                [
                    service(
                        "COACHING",
                        interval("2026-11-02T13:00:00Z", "2026-11-02T14:00:00Z"),
                        available=False,
                    )
                ],
                [],
            ),
            [],
        )

    def test_ties_are_broken_by_service_id(self) -> None:
        same = interval("2026-11-02T13:00:00Z", "2026-11-02T14:00:00Z")
        first = service("COACHING", same, service_id="00000000-0000-4000-8000-000000000001")
        second = service("COACHING", same, service_id="00000000-0000-4000-8000-000000000002")

        result = suggest([second, first], [])

        self.assertEqual(
            [item.service["service_id"] for item in result],
            [first["service_id"], second["service_id"]],
        )

    def test_instants_without_offset_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            instant("2026-11-07T10:00:00")


if __name__ == "__main__":
    unittest.main()
