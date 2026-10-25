"""HU030 (ADR-041): `bff-mobile` publica `GET /v1/me/suggestions/nutrition-routines` sobre el
contrato de `training-management` sin crear una API paralela.

- El cliente móvil solo envía `limit`: patrón, alérgenos, deportes y objetivo los agrega el BFF
  desde Usuarios (las restricciones nunca las envía ni las recibe el cliente).
- Cada campo de la rutina que el BFF entrega conserva el esquema del servicio; el BFF agrega la
  forma común de ADR-041 (`reference`, `title`, `summary`, `author_name`, `missing_context`,
  `notices`, `generated_at`, `sources`).
"""

import unittest
from typing import Any

from test_training_dashboard_contract import _load, _resolved

PATH, METHOD = "/v1/me/suggestions/nutrition-routines", "get"
FORWARDED = (
    "template_id",
    "professional_id",
    "goal",
    "sport",
    "dietary_pattern",
    "contains_allergens",
    "duration_days",
    "published_at",
    "reasons",
)


def _ok(document: dict[str, Any]) -> Any:
    operation = document["paths"][PATH][METHOD]
    return _resolved(
        document, operation["responses"]["200"]["content"]["application/json"]["schema"]
    )


class NutritionRoutineSuggestionsContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.service = _load("training-management")
        cls.bff = _load("bff-mobile")

    def test_client_only_sends_limit(self) -> None:
        def names(document: dict[str, Any]) -> set[str]:
            return {p["name"] for p in document["paths"][PATH][METHOD].get("parameters", [])}

        self.assertEqual(names(self.bff), {"limit", "authorization"})
        self.assertEqual(
            names(self.service),
            {
                "dietary_pattern",
                "exclude_allergens",
                "sport",
                "primary_goal",
                "limit",
                "authorization",
            },
        )

    def test_forwarded_fields_keep_the_service_schema(self) -> None:
        service_item = _ok(self.service)["properties"]["items"]["items"]["properties"]
        bff_item = _ok(self.bff)["properties"]["items"]["items"]["properties"]

        for field in FORWARDED:
            with self.subTest(field=field):
                self.assertEqual(bff_item[field], service_item[field])
        bff_meal = bff_item["daily_meals"]["items"]
        service_meal = service_item["daily_meals"]["items"]
        self.assertEqual(bff_meal["properties"]["meal"], service_meal["properties"]["meal"])
        self.assertEqual(
            bff_meal["properties"]["items"]["items"]["properties"].keys(),
            service_meal["properties"]["items"]["items"]["properties"].keys(),
        )
        # Sin resolver: `_resolved` descarta las claves `title` de documentación.
        published = self.bff["components"]["schemas"]["RoutineSuggestionItem"]["properties"]
        self.assertLessEqual({"reference", "title", "summary", "author_name"}, set(published))

    def test_common_envelope_and_relayed_errors(self) -> None:
        bff, service = _ok(self.bff), _ok(self.service)

        self.assertEqual(
            set(bff["properties"]),
            {
                "items",
                "personalized",
                "missing_context",
                "notices",
                "generated_at",
                "sources",
            },
        )
        self.assertEqual(bff["properties"]["personalized"], service["properties"]["personalized"])
        self.assertEqual(
            bff["properties"]["notices"]["items"]["enum"],
            ["RESTRICTIONS_NOT_VERIFIED", "NOT_MEDICAL_ADVICE"],
        )
        self.assertEqual(
            bff["properties"]["missing_context"]["items"]["enum"],
            ["nutrition_habits", "sports", "primary_goal"],
        )
        self.assertLessEqual(
            {"401", "403", "422", "503", "504"},
            set(self.service["paths"][PATH][METHOD]["responses"])
            & set(self.bff["paths"][PATH][METHOD]["responses"]),
        )


if __name__ == "__main__":
    unittest.main()
