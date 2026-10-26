"""HU014: API del servicio (autenticación, autorización, validación, errores, ETag).

Routers reales sobre casos de uso con dobles en memoria y tokens EdDSA firmados en la
prueba (misma verificación que en producción, sin red ni base de datos).
"""

import unittest
import uuid
from typing import Any

from fakes import NOW, FakePermissions, FixedClock, InMemoryReadStore
from fastapi.testclient import TestClient
from nutrition_fakes import (
    AUTHOR,
    NO_HABITS,
    FakeRestrictions,
    InMemoryNutritionPlanReader,
    InMemoryNutritionStore,
    InMemoryNutritionUnitOfWork,
    StaticFoodCatalog,
    habits,
    item,
    nutrition_body,
)
from test_api import VERIFIER, token

from sportapp_platform.app import create_service_app
from sportapp_platform.config import ServiceSettings
from training_management.api.food_catalog import build_food_catalog_router
from training_management.api.nutrition_plans import build_nutrition_plans_router
from training_management.application.commands.create_nutrition_plan import CreateNutritionPlan
from training_management.application.ports import AuthorizationView
from training_management.application.queries.food_catalog import GetFoodCatalog
from training_management.application.queries.nutrition_plans import (
    GetNutritionPlan,
    ListMyNutritionPlans,
)
from training_management.domain.authorizations.model import TrainingAuthorization

PLANS = "/v1/nutrition-plans"


class _Authorizations:
    def __init__(self, store: InMemoryReadStore) -> None:
        self.store = store

    def get(self, athlete_id: uuid.UUID, professional_id: uuid.UUID) -> AuthorizationView | None:
        return self.store.authorizations.get((athlete_id, professional_id))


class NutritionApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = InMemoryNutritionStore()
        self.read = InMemoryReadStore()
        self.athlete_id, self.professional_id = uuid.uuid4(), uuid.uuid4()
        self.restrictions = FakeRestrictions(habits())
        permissions = FakePermissions({"service_provider": {AUTHOR}})
        catalog = StaticFoodCatalog()
        reader = InMemoryNutritionPlanReader(self.read.nutrition_plans)
        app = create_service_app(
            service_name="training-management",
            title="test",
            api_version="test",
            settings=ServiceSettings(app_env="test"),
            routers=[
                build_nutrition_plans_router(
                    verify_token=_verify,
                    create_plan=CreateNutritionPlan(
                        unit_of_work=lambda: InMemoryNutritionUnitOfWork(self.store),
                        permissions=permissions,
                        catalog=catalog,
                        restrictions=self.restrictions,
                        clock=FixedClock(),
                    ),
                    get_plan=GetNutritionPlan(
                        reader=reader,
                        authorizations=_Authorizations(self.read),
                        permissions=permissions,
                    ),
                    list_plans=ListMyNutritionPlans(reader=reader),
                ),
                build_food_catalog_router(
                    verify_token=_verify, get_catalog=GetFoodCatalog(catalog=catalog)
                ),
            ],
        )
        self.client = TestClient(app)
        self.athlete = _bearer(self.athlete_id, "athlete")
        self.professional = _bearer(self.professional_id, "service_provider")
        self.store.authorizations[(self.athlete_id, self.professional_id)] = (
            TrainingAuthorization.grant(
                athlete_id=self.athlete_id, professional_id=self.professional_id, now=NOW
            )
        )
        self.read.authorizations[(self.athlete_id, self.professional_id)] = AuthorizationView(
            self.athlete_id, self.professional_id, "ACTIVE", 1, NOW, None
        )

    def create(
        self, body: dict[str, Any] | None = None, key: str | None = None, headers: Any = None
    ) -> Any:
        return self.client.post(
            PLANS,
            json=body or nutrition_body(self.athlete_id),
            headers={
                **(headers or self.professional),
                "Idempotency-Key": key or str(uuid.uuid4()),
            },
        )

    def project_all(self) -> None:
        self.read.nutrition_plans.update(self.store.plans)

    def test_creates_plan_with_location_and_normalized_quantities(self) -> None:
        """CA1: `201`, `ASSIGNED`, versión 1, cantidades con un decimal y `Location`."""
        response = self.create()

        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        self.assertEqual(response.headers["Location"], f"{PLANS}/{body['plan_id']}")
        self.assertNotIn("Idempotent-Replayed", response.headers)
        self.assertEqual((body["status"], body["version"]), ("ASSIGNED", 1))
        self.assertEqual(body["professional_id"], str(self.professional_id))
        self.assertEqual(body["catalog_version"], "food-catalog-2026-10-13.v1")
        self.assertEqual(
            body["meals"][0],
            {
                "meal_type": "BREAKFAST",
                "items": [
                    {"food_code": "OATS", "quantity": "80.0", "unit": "G"},
                    {"food_code": "BANANA", "quantity": "1.0", "unit": "UNIT"},
                ],
            },
        )
        self.assertEqual(body["warnings"], [])
        self.assertTrue(body["created_at"].endswith("Z"))

    def test_retry_replays_with_header_and_warnings(self) -> None:
        """CA1 y CA6: el reintento devuelve el mismo plan y los mismos `warnings`."""
        self.restrictions.result = NO_HABITS
        key = str(uuid.uuid4())

        first, again = self.create(key=key), self.create(key=key)

        self.assertEqual((first.status_code, again.status_code), (201, 201))
        self.assertEqual(again.headers["Idempotent-Replayed"], "true")
        self.assertEqual(again.json(), first.json())
        self.assertEqual(first.json()["warnings"], ["nutrition_habits_missing"])
        self.assertEqual(len(self.store.plans), 1)

    def test_key_reused_with_other_body_is_a_conflict(self) -> None:
        key = str(uuid.uuid4())
        self.create(key=key)

        response = self.create(nutrition_body(self.athlete_id, title="Otro"), key=key)

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "idempotency_key_reused")

    def test_authentication_and_permissions(self) -> None:
        """CA3: sin token → 401; deportista u otro actor → 403; sin autorización → 403."""
        unauthenticated = self.client.post(
            PLANS,
            json=nutrition_body(self.athlete_id),
            headers={"Idempotency-Key": str(uuid.uuid4())},
        )
        athlete = self.create(headers=self.athlete)
        other = self.create(nutrition_body(uuid.uuid4()))

        self.assertEqual(unauthenticated.status_code, 401)
        self.assertEqual(unauthenticated.headers["WWW-Authenticate"], "Bearer")
        self.assertEqual(
            (athlete.status_code, athlete.json()["code"]), (403, "permission_required")
        )
        self.assertEqual(
            (other.status_code, other.json()["code"]), (403, "athlete_authorization_required")
        )
        self.assertEqual(self.restrictions.calls, [])
        self.assertEqual(self.store.plans, {})

    def test_shape_errors_are_validation_errors(self) -> None:
        cases = [
            nutrition_body(self.athlete_id, extra="x"),
            nutrition_body(self.athlete_id, athlete_email="a@example.com"),
            nutrition_body(self.athlete_id, title=""),
            nutrition_body(self.athlete_id, objectives=[]),
            nutrition_body(self.athlete_id, meals=[]),
            nutrition_body(self.athlete_id, meals=[{"meal_type": "BRUNCH", "items": [item()]}]),
            nutrition_body(self.athlete_id, meals=[{"meal_type": "LUNCH", "items": []}]),
            nutrition_body(
                self.athlete_id,
                meals=[{"meal_type": "LUNCH", "items": [{**item(), "quantity": 80}]}],
            ),
            nutrition_body(
                self.athlete_id, meals=[{"meal_type": "LUNCH", "items": [item(quantity="-1")]}]
            ),
            nutrition_body(
                self.athlete_id, meals=[{"meal_type": "LUNCH", "items": [item(unit="KG")]}]
            ),
            nutrition_body(self.athlete_id, meals=[{"meal_type": "LUNCH", "items": [item()] * 16}]),
        ]
        for index, body in enumerate(cases):
            with self.subTest(case=index):
                response = self.create(body)
                self.assertEqual(response.status_code, 422, response.text)
                self.assertEqual(response.json()["code"], "validation_error")
        missing_key = self.client.post(
            PLANS, json=nutrition_body(self.athlete_id), headers=self.professional
        )
        self.assertEqual(missing_key.status_code, 422)
        self.assertEqual(self.store.plans, {})

    def test_rule_errors_list_every_field(self) -> None:
        body = nutrition_body(
            self.athlete_id,
            end_date="2026-11-01",
            meals=[
                {
                    "meal_type": "LUNCH",
                    "items": [item("PIZZA"), item("WATER", "10", "G"), item("OATS", "1.25")],
                }
            ],
        )

        response = self.create(body)

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["code"], "nutrition_plan_invalid")
        self.assertEqual(
            response.json()["details"],
            [
                {"field": "body.end_date", "issue": "end_date_before_start_date"},
                {"field": "body.meals.0.items.0.food_code", "issue": "unknown_value"},
                {"field": "body.meals.0.items.1.unit", "issue": "unit_not_allowed"},
                {"field": "body.meals.0.items.2.quantity", "issue": "out_of_range"},
            ],
        )
        self.assertEqual(self.restrictions.calls, [])

    def test_conflicts_are_reported_per_item_and_cause(self) -> None:
        """CA2: bloqueo por alergia y restricción, sin plan."""
        self.restrictions.result = habits(diet="VEGAN", allergies=("MILK",))
        body = nutrition_body(
            self.athlete_id,
            meals=[{"meal_type": "BREAKFAST", "items": [item("OATS"), item("MILK", "200", "ML")]}],
        )

        response = self.create(body)

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["code"], "nutrition_plan_invalid")
        self.assertEqual(
            response.json()["details"],
            [
                {"field": "body.meals.0.items.1.food_code", "issue": "conflicts_with_allergy"},
                {"field": "body.meals.0.items.1.food_code", "issue": "conflicts_with_restriction"},
            ],
        )
        self.assertEqual(self.store.plans, {})

    def test_users_unavailable_is_503(self) -> None:
        """CA6: Usuarios caído → `503 upstream_unavailable` sin plan."""
        self.restrictions.unavailable = True

        response = self.create()

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["code"], "upstream_unavailable")
        self.assertEqual(self.store.plans, {})

    def test_get_plan_for_creator_and_owner(self) -> None:
        plan_id = self.create().json()["plan_id"]
        pending = self.client.get(f"{PLANS}/{plan_id}", headers=self.athlete)
        self.project_all()

        by_owner = self.client.get(f"{PLANS}/{plan_id}", headers=self.athlete)
        by_creator = self.client.get(f"{PLANS}/{plan_id}", headers=self.professional)

        self.assertEqual(pending.status_code, 404)
        self.assertEqual(pending.json()["code"], "nutrition_plan_not_found")
        self.assertEqual((by_owner.status_code, by_creator.status_code), (200, 200))
        self.assertEqual(by_owner.headers["Cache-Control"], "no-store")
        self.assertEqual(by_owner.json(), by_creator.json())
        self.assertNotIn("warnings", by_owner.json())

    def test_plan_is_immutable_in_version_one(self) -> None:
        """CA4: el plan nace en versión 1 y no existe operación de edición ni borrado."""
        created = self.create().json()
        self.project_all()
        path = f"{PLANS}/{created['plan_id']}"

        responses = [
            self.client.put(path, json=nutrition_body(self.athlete_id), headers=self.professional),
            self.client.patch(path, json={"title": "Otro"}, headers=self.professional),
            self.client.delete(path, headers=self.professional),
        ]

        self.assertEqual([response.status_code for response in responses], [405, 405, 405])
        current = self.client.get(path, headers=self.professional).json()
        self.assertEqual((current["version"], current["title"]), (1, created["title"]))

    def test_get_plan_rejections(self) -> None:
        """CA3: tercero → 404; otro actor → 403; creador con autorización revocada → 403."""
        plan_id = self.create().json()["plan_id"]
        self.project_all()

        stranger = self.client.get(f"{PLANS}/{plan_id}", headers=_bearer(uuid.uuid4(), "athlete"))
        organizer = self.client.get(
            f"{PLANS}/{plan_id}", headers=_bearer(uuid.uuid4(), "organizer")
        )
        self.read.authorizations.clear()
        revoked = self.client.get(f"{PLANS}/{plan_id}", headers=self.professional)
        anonymous = self.client.get(f"{PLANS}/{plan_id}")

        self.assertEqual(
            (stranger.status_code, stranger.json()["code"]), (404, "nutrition_plan_not_found")
        )
        self.assertEqual(
            (organizer.status_code, organizer.json()["code"]), (403, "permission_required")
        )
        self.assertEqual(
            (revoked.status_code, revoked.json()["code"]), (403, "athlete_authorization_required")
        )
        self.assertEqual(anonymous.status_code, 401)

    def test_my_plans(self) -> None:
        for _ in range(3):
            self.create()
        self.project_all()

        first = self.client.get("/v1/me/nutrition-plans?limit=2", headers=self.athlete)
        second = self.client.get(
            f"/v1/me/nutrition-plans?limit=2&cursor={first.json()['next_cursor']}",
            headers=self.athlete,
        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.headers["Cache-Control"], "no-store")
        self.assertEqual(len(first.json()["items"]), 2)
        self.assertEqual((len(second.json()["items"]), second.json()["next_cursor"]), (1, None))
        ids = [plan["plan_id"] for plan in first.json()["items"] + second.json()["items"]]
        self.assertEqual(len(set(ids)), 3)

    def test_my_plans_errors(self) -> None:
        for query, field, issue in (
            ("limit=0", "query.limit", "out_of_range"),
            ("limit=51", "query.limit", "out_of_range"),
            ("limit=abc", "query.limit", "out_of_range"),
            ("cursor=%25%25", "query.cursor", "invalid_cursor"),
        ):
            with self.subTest(query=query):
                response = self.client.get(f"/v1/me/nutrition-plans?{query}", headers=self.athlete)
                self.assertEqual(response.status_code, 422)
                self.assertEqual(
                    (response.json()["code"], response.json()["details"]),
                    ("validation_error", [{"field": field, "issue": issue}]),
                )
        professional = self.client.get("/v1/me/nutrition-plans", headers=self.professional)
        self.assertEqual(
            (professional.status_code, professional.json()["code"]), (403, "permission_required")
        )
        self.assertEqual(self.client.get("/v1/me/nutrition-plans").status_code, 401)

    def test_food_catalog_with_etag(self) -> None:
        response = self.client.get("/v1/food-catalog", headers=self.professional)
        etag = response.headers["ETag"]
        cached = self.client.get(
            "/v1/food-catalog", headers={**self.athlete, "If-None-Match": f'W/{etag}, "x"'}
        )
        stale = self.client.get(
            "/v1/food-catalog", headers={**self.athlete, "If-None-Match": '"otro"'}
        )
        anonymous = self.client.get("/v1/food-catalog")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(etag, '"food-catalog-2026-10-13.v1"')
        body = response.json()
        self.assertEqual(body["catalog_version"], "food-catalog-2026-10-13.v1")
        self.assertEqual(len(body["items"]), 41)
        peanut = next(food for food in body["items"] if food["food_code"] == "PEANUT_BUTTER")
        self.assertEqual(
            peanut,
            {
                "food_code": "PEANUT_BUTTER",
                "label_key": "food.peanut_butter",
                "category": "FAT",
                "default_unit": "G",
                "allowed_units": ["G", "TBSP"],
                "allergens": ["PEANUTS"],
                "incompatible_with": [],
            },
        )
        self.assertEqual((cached.status_code, cached.content), (304, b""))
        self.assertEqual(cached.headers["ETag"], etag)
        self.assertEqual(stale.status_code, 200)
        self.assertEqual(anonymous.status_code, 401)


def _verify(raw: str) -> Any:
    return VERIFIER.verify(raw)


def _bearer(user_id: uuid.UUID, actor_type: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token(user_id, actor_type)}"}


if __name__ == "__main__":
    unittest.main()
