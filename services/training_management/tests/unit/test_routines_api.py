"""HU030 (ADR-041): API del servicio para rutinas alimenticias (autenticación, autorización,
validación por campo con el catálogo de alimentos, idempotencia, retiro, consulta propia y
sugerencias con exclusión por alérgeno y patrón) con routers reales, casos de uso con dobles en
memoria y tokens EdDSA firmados en la prueba."""

import unittest
import uuid
from datetime import timedelta
from typing import Any

from fakes import FakeCapabilities, FakePermissions, FixedClock
from fastapi.testclient import TestClient
from leak_checks import leaks
from nutrition_fakes import StaticFoodCatalog
from routine_fakes import (
    AUTHOR,
    SUGGEST,
    InMemoryRoutineReader,
    InMemoryRoutineStore,
    InMemoryRoutineUnitOfWork,
    routine_body,
)
from test_api import VERIFIER, token

from sportapp_platform.app import create_service_app
from sportapp_platform.config import ServiceSettings
from training_management.api.nutrition_routine_templates import (
    build_nutrition_routine_templates_router,
)
from training_management.application.commands.nutrition_routine_templates import (
    PublishRoutine,
    RetireRoutine,
)
from training_management.application.errors import CapabilitiesTimeout
from training_management.application.queries.nutrition_routine_templates import (
    ListMyRoutines,
    SuggestRoutines,
)
from training_management.domain.routines.events import (
    PUBLISHED_EVENT_TYPE,
    RETIRED_EVENT_TYPE,
)

BASE = "/v1/nutrition-routine-templates"
MINE = "/v1/me/nutrition-routine-templates"
SUGGESTIONS = "/v1/me/suggestions/nutrition-routines"


class _SlowCapabilities(FakeCapabilities):
    def has_capability(self, user_id: uuid.UUID, capability: str) -> bool:
        raise CapabilitiesTimeout()


def _bearer(user_id: uuid.UUID, actor_type: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token(user_id, actor_type)}"}


def _meals(*codes: str) -> list[dict[str, Any]]:
    return [{"meal": "LUNCH", "items": [{"food_code": code, "portions": 1} for code in codes]}]


class RoutinesApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = InMemoryRoutineStore()
        self.clock = FixedClock()
        self.professional_id, self.athlete_id = uuid.uuid4(), uuid.uuid4()
        self.capabilities = FakeCapabilities({self.athlete_id: {SUGGEST}})
        self.reader = InMemoryRoutineReader(self.store.routines)
        self.catalog = StaticFoodCatalog()
        self.client = self.client_with(self.capabilities)
        self.professional = _bearer(self.professional_id, "service_provider")
        self.athlete = _bearer(self.athlete_id, "athlete")

    def client_with(self, capabilities: FakeCapabilities) -> TestClient:
        permissions = FakePermissions({"service_provider": {AUTHOR}})
        app = create_service_app(
            service_name="training-management",
            title="test",
            api_version="test",
            settings=ServiceSettings(app_env="test"),  # type: ignore[call-arg]
            routers=[
                build_nutrition_routine_templates_router(
                    verify_token=VERIFIER.verify,
                    publish=PublishRoutine(
                        unit_of_work=lambda: InMemoryRoutineUnitOfWork(self.store),
                        permissions=permissions,
                        catalog=self.catalog,
                        clock=self.clock,
                    ),
                    retire=RetireRoutine(
                        unit_of_work=lambda: InMemoryRoutineUnitOfWork(self.store),
                        permissions=permissions,
                        clock=self.clock,
                    ),
                    list_routines=ListMyRoutines(reader=self.reader, permissions=permissions),
                    suggest=SuggestRoutines(reader=self.reader, capabilities=capabilities),
                )
            ],
        )
        return TestClient(app)

    def publish(
        self, body: dict[str, Any] | None = None, key: str | None = None, headers: Any = None
    ) -> Any:
        return self.client.post(
            BASE,
            json=body or routine_body(),
            headers={
                **(headers or self.professional),
                "Idempotency-Key": key or str(uuid.uuid4()),
            },
        )

    def retire(self, template_id: str, version: int = 1, headers: Any = None) -> Any:
        return self.client.post(
            f"{BASE}/{template_id}/retire",
            json={"expected_version": version},
            headers=headers or self.professional,
        )

    def test_publishes_idempotently_with_location_and_event(self) -> None:
        """CA4: `201` `PUBLISHED` v1 con autor y vigencia; el reintento no publica otra; otra
        rutina con la misma clave → `409`."""
        key = str(uuid.uuid4())
        with self.assertLogs("sportapp.audit", level="INFO") as audit:
            first = self.publish(key=key)
            again = self.publish(key=key)
            other = self.publish(routine_body(title="Otra rutina"), key=key)

        self.assertEqual(first.status_code, 201, first.text)
        body = first.json()
        self.assertEqual(first.headers["Location"], f"{BASE}/{body['template_id']}")
        self.assertEqual(
            (body["status"], body["version"], body["professional_id"], body["duration_days"]),
            ("PUBLISHED", 1, str(self.professional_id), 14),
        )
        self.assertEqual(body["daily_meals"], routine_body()["daily_meals"])
        self.assertEqual(body["published_at"], "2026-10-12T12:00:00Z")
        self.assertEqual((again.json(), again.headers["Idempotent-Replayed"]), (body, "true"))
        self.assertEqual((other.status_code, other.json()["code"]), (409, "idempotency_key_reused"))
        [event] = self.store.outbox
        self.assertEqual(
            (event.event_type, event.aggregate_type, event.payload),
            (PUBLISHED_EVENT_TYPE, "nutrition_routine_template", body),
        )
        fields = [record.__dict__["fields"] for record in audit.records]
        self.assertEqual([item["result"] for item in fields], ["published", "replayed", "rejected"])
        for record_fields in fields:
            self.assertLessEqual(
                set(record_fields), {"professional_id", "result", "reason", "template_id"}
            )
            sensitive = ("Desayunos", "OATS", "BANANA", "LENTILS", "VEGETARIAN", "Bearer")
            self.assertEqual(leaks(record_fields, sensitive), [])

    def test_catalog_allergens_are_added_to_the_declared_ones(self) -> None:
        body = self.publish(
            routine_body(
                dietary_pattern="OMNIVORE",
                contains_allergens=["SESAME"],
                daily_meals=_meals("SALMON", "WHOLE_WHEAT_BREAD"),
            )
        ).json()

        self.assertEqual(body["contains_allergens"], ["WHEAT", "FISH", "SESAME"])

    def test_invalid_routines_are_422_by_field_and_nothing_is_published(self) -> None:
        """CA4: `food_code` inexistente → `422`; validaciones por campo."""
        missing = routine_body()
        del missing["contains_allergens"]
        cases: dict[str, tuple[dict[str, Any], tuple[str, str]]] = {
            "alimento inexistente": (
                routine_body(daily_meals=_meals("OATS", "PIZZA")),
                ("body.daily_meals.0.items.1.food_code", "unknown_value"),
            ),
            "alimento incompatible con el patrón": (
                routine_body(dietary_pattern="VEGAN", daily_meals=_meals("EGG")),
                ("body.daily_meals.0.items.0.food_code", "conflicts_with_restriction"),
            ),
            "alérgeno repetido": (
                routine_body(contains_allergens=["FISH", "FISH"]),
                ("body.contains_allergens", "duplicate_value"),
            ),
            "alérgenos faltantes": (missing, ("body.contains_allergens", "missing")),
            "alérgeno desconocido": (
                routine_body(contains_allergens=["GLUTEN"]),
                ("body.contains_allergens.0", "enum"),
            ),
            "patrón": (routine_body(dietary_pattern="PALEO"), ("body.dietary_pattern", "enum")),
            "título corto": (routine_body(title="ab"), ("body.title", "string_too_short")),
            "días": (routine_body(duration_days=91), ("body.duration_days", "less_than_equal")),
            "sin comidas": (routine_body(daily_meals=[]), ("body.daily_meals", "too_short")),
            "porciones": (
                routine_body(
                    daily_meals=[{"meal": "LUNCH", "items": [{"food_code": "OATS", "portions": 0}]}]
                ),
                ("body.daily_meals.0.items.0.portions", "greater_than_equal"),
            ),
            "comida": (
                routine_body(
                    daily_meals=[
                        {"meal": "BRUNCH", "items": [{"food_code": "OATS", "portions": 1}]}
                    ]
                ),
                ("body.daily_meals.0.meal", "enum"),
            ),
            "precio": (routine_body(price="10000"), ("body.price", "extra_forbidden")),
        }
        for name, (body, expected) in cases.items():
            with self.subTest(name=name):
                response = self.publish(body)

                self.assertEqual(response.status_code, 422, response.text)
                self.assertEqual(response.json()["code"], "validation_error")
                self.assertIn(
                    expected,
                    {(item["field"], item["issue"]) for item in response.json()["details"]},
                )
        self.assertEqual((self.store.routines, self.store.outbox), ({}, []))

    def test_only_nutrition_plan_authors_publish_retire_and_list(self) -> None:
        for actor in ("athlete", "organizer"):
            headers = _bearer(uuid.uuid4(), actor)
            with self.subTest(actor=actor):
                for response in (
                    self.publish(headers=headers),
                    self.retire(str(uuid.uuid4()), headers=headers),
                    self.client.get(MINE, headers=headers),
                ):
                    self.assertEqual(
                        (response.status_code, response.json()["code"]),
                        (403, "permission_required"),
                    )
        self.assertEqual(
            self.client.post(
                BASE, json=routine_body(), headers={"Idempotency-Key": str(uuid.uuid4())}
            ).status_code,
            401,
        )
        self.assertEqual(self.catalog.loads, 0, "sin permiso no se consulta el catálogo")

    def test_retire_foreign_stale_and_repeated(self) -> None:
        """CA4: retiro idempotente; ajena → `404`; versión vieja → `409`."""
        template_id = self.publish().json()["template_id"]
        self.clock.advance(timedelta(hours=1))

        foreign = self.retire(template_id, headers=_bearer(uuid.uuid4(), "service_provider"))
        stale = self.retire(template_id, version=2)
        retired = self.retire(template_id)
        again = self.retire(template_id)

        self.assertEqual((foreign.status_code, foreign.json()["code"]), (404, "template_not_found"))
        self.assertEqual(
            (stale.status_code, stale.json()["code"], stale.json()["details"]),
            (409, "version_conflict", [{"field": "expected_version", "issue": "1"}]),
        )
        self.assertEqual((retired.json()["status"], retired.json()["version"]), ("RETIRED", 2))
        self.assertEqual((again.status_code, again.json()), (200, retired.json()))
        self.assertEqual(
            [event.event_type for event in self.store.outbox],
            [PUBLISHED_EVENT_TYPE, RETIRED_EVENT_TYPE],
        )
        self.assertEqual(self.retire(str(uuid.uuid4())).status_code, 404)

    def test_my_routines_page_without_losing_or_repeating(self) -> None:
        ids = []
        for _ in range(5):
            self.clock.advance(timedelta(minutes=1))
            ids.append(self.publish().json()["template_id"])
        self.publish(headers=_bearer(uuid.uuid4(), "service_provider"))

        seen, cursor = [], None
        while True:
            params = {"limit": "2", **({"cursor": cursor} if cursor else {})}
            response = self.client.get(MINE, params=params, headers=self.professional)
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            seen.extend(item["template_id"] for item in response.json()["items"])
            cursor = response.json()["next_cursor"]
            if cursor is None:
                break

        self.assertEqual(seen, list(reversed(ids)))
        for params, field, issue in (
            ({"cursor": "x"}, "query.cursor", "invalid_cursor"),
            ({"limit": "dos"}, "query.limit", "out_of_range"),
        ):
            invalid = self.client.get(MINE, params=params, headers=self.professional)
            self.assertEqual(invalid.json()["details"][0], {"field": field, "issue": issue})

    def seed(self) -> dict[str, str]:
        names = {}
        for title, goal, sport, pattern, codes in (
            ("Salmón y arroz", "PERFORMANCE", "running", "PESCATARIAN", ("SALMON", "WHITE_RICE")),
            ("Avena vegana", "HEALTH", None, "VEGAN", ("OATS", "BERRIES")),
            ("Huevos y pan", "PERFORMANCE", "cycling", "VEGETARIAN", ("EGG", "PASTA")),
            ("Carne y arepa", "PERFORMANCE", "running", "OMNIVORE", ("BEEF", "CORN_AREPA")),
            ("Lentejas", "RECREATION", "running", "VEGAN", ("LENTILS", "SPINACH")),
        ):
            self.clock.advance(timedelta(minutes=1))
            body = routine_body(
                title=title,
                goal=goal,
                sport=sport,
                dietary_pattern=pattern,
                daily_meals=_meals(*codes),
            )
            response = self.publish(body)
            self.assertEqual(response.status_code, 201, response.text)
            names[title] = response.json()["template_id"]
        return names

    def suggestions(self, headers: Any = None, **params: Any) -> Any:
        return self.client.get(SUGGESTIONS, params=params, headers=headers or self.athlete)

    def test_personalized_suggestions_exclude_allergens_and_patterns(self) -> None:
        """CA1 y CA2: solo compatibles con patrón y sin alérgenos, objetivo y deporte primero."""
        self.seed()

        response = self.suggestions(
            dietary_pattern="VEGETARIAN",
            exclude_allergens=["WHEAT"],
            sport=["running"],
            primary_goal="PERFORMANCE",
        )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        body = response.json()
        self.assertTrue(body["personalized"])
        # Fuera: pescado (patrón), carne (patrón) y la de trigo (alergia).
        self.assertEqual(
            [(item["title"], item["reasons"]) for item in body["items"]],
            [
                ("Lentejas", ["RESTRICTION_COMPATIBLE", "SPORT_MATCH"]),
                ("Avena vegana", ["RESTRICTION_COMPATIBLE"]),
            ],
        )
        self.assertEqual(self.capabilities.calls, [(self.athlete_id, SUGGEST)])

    def test_omnivore_receives_goal_matches_first(self) -> None:
        self.seed()

        body = self.suggestions(
            dietary_pattern="OMNIVORE", sport=["cycling"], primary_goal="PERFORMANCE"
        ).json()

        self.assertEqual(
            [item["title"] for item in body["items"]],
            ["Huevos y pan", "Carne y arepa", "Salmón y arroz", "Lentejas", "Avena vegana"],
        )
        self.assertEqual(
            body["items"][0]["reasons"], ["RESTRICTION_COMPATIBLE", "GOAL_MATCH", "SPORT_MATCH"]
        )

    def test_without_habits_the_list_is_not_personalized_and_retired_is_hidden(self) -> None:
        """CA3: sin `dietary_pattern` → no personalizada, sin motivos; retirada no aparece."""
        names = self.seed()
        self.retire(names["Lentejas"])

        body = self.suggestions(sport=["running"], primary_goal="PERFORMANCE").json()

        self.assertFalse(body["personalized"])
        self.assertEqual(
            [item["title"] for item in body["items"]],
            ["Carne y arepa", "Huevos y pan", "Avena vegana", "Salmón y arroz"],
        )
        self.assertTrue(all(item["reasons"] == [] for item in body["items"]))

    def test_suggestions_require_athlete_capability_and_valid_filters(self) -> None:
        without = self.suggestions(headers=_bearer(uuid.uuid4(), "athlete"))
        provider = self.suggestions(headers=self.professional)
        self.assertEqual(
            (without.status_code, without.json()["code"]), (403, "capability_required")
        )
        self.assertEqual(
            (provider.status_code, provider.json()["code"]), (403, "permission_required")
        )
        calls = len(self.capabilities.calls)
        for field, issue, params in (
            ("query.limit", "out_of_range", {"limit": "51"}),
            ("query.dietary_pattern", "required", {"exclude_allergens": ["FISH"]}),
            (
                "query.exclude_allergens",
                "duplicate_value",
                {"dietary_pattern": "VEGAN", "exclude_allergens": ["FISH", "FISH"]},
            ),
            ("query.sport", "duplicate_value", {"sport": ["running", "running"]}),
        ):
            with self.subTest(field=field, issue=issue):
                response = self.suggestions(**params)
                self.assertEqual(response.status_code, 422)
                self.assertEqual(response.json()["details"][0], {"field": field, "issue": issue})
        for field, params in (
            ("query.dietary_pattern", {"dietary_pattern": "PALEO"}),
            ("query.exclude_allergens.0", {"exclude_allergens": ["GLUTEN"]}),
        ):
            with self.subTest(field=field):
                response = self.suggestions(**params)
                self.assertEqual(response.status_code, 422)
                self.assertEqual(response.json()["details"][0]["field"], field)
        self.assertEqual(len(self.capabilities.calls), calls)
        self.capabilities.unavailable = True
        self.assertEqual(self.suggestions().status_code, 503)
        slow = self.client_with(_SlowCapabilities()).get(SUGGESTIONS, headers=self.athlete)
        self.assertEqual(slow.status_code, 504)


if __name__ == "__main__":
    unittest.main()
