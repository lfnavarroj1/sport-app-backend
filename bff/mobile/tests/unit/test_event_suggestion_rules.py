"""Reglas de HU027 (ADR-041) sin transporte: normalización, alias disciplina y deporte, eventos
elegibles, motivos y orden determinista."""

import json
import unittest
import uuid
from datetime import UTC, datetime, timedelta
from importlib.resources import files
from typing import Any

from bff_mobile.api.event_suggestion_rules import (
    CITY_MATCH,
    SPORT_MATCH,
    UPCOMING,
    athlete_match,
    load_aliases,
    normalize,
    suggest,
)

NOW = datetime(2026, 10, 24, 12, tzinfo=UTC)
ALIASES = load_aliases()


def event(
    discipline: str, city: str, days: float, *, status: str = "PUBLISHED", event_id: str = ""
) -> dict[str, Any]:
    starts_at = NOW + timedelta(days=days)
    return {
        "event_id": event_id or str(uuid.uuid4()),
        "name": f"{discipline} en {city}",
        "discipline": discipline,
        "location": {"name": "Parque", "city": city, "country_code": "CO"},
        "starts_at": starts_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ends_at": (starts_at + timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "time_zone": "America/Bogota",
        "status": status,
        "version": 2,
        "fee": None,
    }


class NormalizationTest(unittest.TestCase):
    def test_lowercase_without_accents_and_trimmed(self) -> None:
        for raw in ("Natación", "natacion", "  NATACIÓN ", "Natación"):
            with self.subTest(raw=raw):
                self.assertEqual(normalize(raw), "natacion")
        self.assertEqual(normalize("  Aguas   Abiertas "), "aguas abiertas")
        self.assertEqual(normalize("Bogotá D.C."), "bogota d.c.")

    def test_aliases_of_adr_041(self) -> None:
        swimmer = athlete_match(["swimming"], "Bogotá", ALIASES)
        for discipline in ("Natación", "natacion", "Aguas abiertas", "SWIMMING"):
            with self.subTest(discipline=discipline):
                self.assertIn(normalize(discipline), swimmer.disciplines)
        self.assertEqual(
            ALIASES["running"],
            {"running", "carrera", "atletismo", "trail", "maraton", "media maraton", "5k", "10k"},
        )
        self.assertEqual(ALIASES["cycling"], {"cycling", "ciclismo", "mtb", "ruta", "gran fondo"})
        self.assertEqual(ALIASES["strength"], {"strength", "fuerza", "crossfit", "funcional"})

    def test_other_never_matches(self) -> None:
        self.assertNotIn("other", ALIASES)
        match = athlete_match(["other"], "Medellín", ALIASES)
        self.assertEqual(match.disciplines, frozenset())
        (item,) = suggest([event("Otro", "Cali", 3)], match, NOW)
        self.assertEqual(item.reasons, (UPCOMING,))

    def test_alias_file_is_versioned_and_validated(self) -> None:
        document = json.loads(
            files("bff_mobile.api").joinpath("discipline-aliases.v1.json").read_text("utf-8")
        )
        self.assertEqual(document["version"], "discipline-aliases.v1")
        for broken in (
            {**document, "version": "discipline-aliases.v0"},
            {**document, "sports": {"running": ["Carrera"]}},
            {**document, "sports": {"running": ["trail"], "cycling": ["trail"]}},
            {**document, "sports": {"other": ["otro"]}},
        ):
            with self.subTest(broken=broken["sports"]), self.assertRaises(ValueError):
                load_aliases(broken)


class SuggestTest(unittest.TestCase):
    def test_order_by_both_matches_then_sport_then_city_then_date(self) -> None:
        """CA1: coincidencia de deporte y ciudad primero, luego por fecha; motivos estables."""
        match = athlete_match(["running", "swimming"], "bogota", ALIASES)
        later_both = event("Media Maratón", "Bogotá", 20)
        sooner_both = event("natación", "BOGOTÁ", 10)
        sport_only = event("Trail", "Medellín", 1)
        city_only = event("Ciclismo", "Bogota", 2)
        unrelated = event("Ajedrez", "Cali", 0.5)

        result = suggest([unrelated, city_only, sport_only, later_both, sooner_both], match, NOW)

        self.assertEqual(
            [(item.event["event_id"], item.reasons) for item in result],
            [
                (sooner_both["event_id"], (SPORT_MATCH, CITY_MATCH)),
                (later_both["event_id"], (SPORT_MATCH, CITY_MATCH)),
                (sport_only["event_id"], (SPORT_MATCH,)),
                (city_only["event_id"], (CITY_MATCH,)),
                (unrelated["event_id"], (UPCOMING,)),
            ],
        )

    def test_same_start_breaks_ties_by_event_id(self) -> None:
        match = athlete_match(["cycling"], "Cali", ALIASES)
        second = event("MTB", "Cali", 5, event_id="ffffffff-0000-4000-8000-000000000000")
        first = event("MTB", "Cali", 5, event_id="00000000-0000-4000-8000-000000000000")

        result = suggest([second, first], match, NOW)

        self.assertEqual([item.event for item in result], [first, second])

    def test_not_personalized_is_by_date_with_upcoming(self) -> None:
        """CA2: sin contexto, próximos eventos por fecha y nunca con motivos de coincidencia."""
        later, sooner = event("Running", "Bogotá", 9), event("Natación", "Cali", 4)

        result = suggest([later, sooner], None, NOW)

        self.assertEqual([item.event for item in result], [sooner, later])
        self.assertTrue(all(item.reasons == (UPCOMING,) for item in result))

    def test_started_or_unpublished_events_are_not_suggested(self) -> None:
        """CA3: un evento ya iniciado o no publicado no aparece."""
        started = event("Running", "Bogotá", -0.01)
        exactly_now = event("Running", "Bogotá", 0)
        draft = event("Running", "Bogotá", 3, status="DRAFT")
        upcoming = event("Running", "Bogotá", 3)

        result = suggest([started, exactly_now, draft, upcoming], None, NOW)

        self.assertEqual([item.event for item in result], [upcoming])


if __name__ == "__main__":
    unittest.main()
