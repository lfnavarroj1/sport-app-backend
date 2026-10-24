"""HU026 (ADR-041): `bff-mobile` publica `GET /v1/me/suggestions/routes` sobre el contrato de
`training-management` sin crear una API paralela.

- El cliente móvil solo envía `limit`: deportes, ciudad y país los agrega el BFF desde el
  contexto interno de Usuarios.
- Cada campo de la ruta que el BFF entrega tiene el mismo esquema que en el servicio; el BFF
  solo agrega la forma común de ADR-041 (`reference`, `title`, `summary`, `author_name`,
  `missing_context`, `generated_at`, `sources`) y no expone `organizer_id`.
- Los errores del servicio que el BFF reenvía están publicados en ambos contratos.
"""

import unittest
from typing import Any

from test_training_dashboard_contract import _load, _resolved

PATH, METHOD = "/v1/me/suggestions/routes", "get"
FORWARDED = (
    "route_id",
    "sport",
    "distance_km",
    "difficulty",
    "city",
    "country_code",
    "start_point",
    "waypoints",
    "source",
    "published_at",
    "reasons",
)


def _ok(document: dict[str, Any]) -> Any:
    operation = document["paths"][PATH][METHOD]
    return _resolved(
        document, operation["responses"]["200"]["content"]["application/json"]["schema"]
    )


class RouteSuggestionsContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.service = _load("training-management")
        cls.bff = _load("bff-mobile")

    def test_client_only_sends_limit_and_the_bff_adds_the_context_filters(self) -> None:
        def names(document: dict[str, Any]) -> set[str]:
            return {p["name"] for p in document["paths"][PATH][METHOD].get("parameters", [])}

        self.assertEqual(names(self.bff), {"limit", "authorization"})
        self.assertEqual(
            names(self.service), {"sport", "city", "country_code", "limit", "authorization"}
        )

    def test_forwarded_route_fields_keep_the_service_schema(self) -> None:
        service_item = _ok(self.service)["properties"]["items"]["items"]["properties"]
        bff_item = _ok(self.bff)["properties"]["items"]["items"]["properties"]

        for field in FORWARDED:
            with self.subTest(field=field):
                self.assertEqual(bff_item[field], service_item[field])
        # Sin resolver: `_resolved` descarta las claves `title` de documentación.
        published = self.bff["components"]["schemas"]["RouteSuggestionItem"]["properties"]
        self.assertNotIn("organizer_id", published)
        self.assertLessEqual({"reference", "title", "summary", "author_name"}, set(published))

    def test_common_suggestion_envelope_of_adr_041(self) -> None:
        bff, service = _ok(self.bff), _ok(self.service)

        self.assertEqual(
            set(bff["properties"]),
            {"items", "personalized", "missing_context", "generated_at", "sources"},
        )
        self.assertEqual(bff["properties"]["personalized"], service["properties"]["personalized"])

    def test_relayed_errors_are_published_in_both_contracts(self) -> None:
        service = set(self.service["paths"][PATH][METHOD]["responses"])
        bff = set(self.bff["paths"][PATH][METHOD]["responses"])

        self.assertLessEqual({"401", "403", "422", "503", "504"}, service & bff)


if __name__ == "__main__":
    unittest.main()
