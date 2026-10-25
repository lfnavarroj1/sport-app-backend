"""API de HU013 en el servicio: historial de alimentación con dobles en memoria y tokens."""

import unittest
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from fakes import NOW, InMemoryAuthorizationReader, InMemoryReadStore
from fastapi.testclient import TestClient
from intake_fakes import InMemoryFoodIntakeReader, stored_intake
from test_api import VERIFIER, token
from test_food_intake_history import PERMISSIONS, authorization

from sportapp_platform.app import create_service_app
from sportapp_platform.config import ServiceSettings
from training_management.api.food_intake_history import build_food_intake_history_router
from training_management.application.queries.food_intakes import GetFoodIntakeHistory
from training_management.domain.nutrition.model import MealType

OWN = "/v1/me/food-intakes"


class FoodIntakeHistoryApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.read = InMemoryReadStore()
        self.athlete_id, self.professional_id = uuid4(), uuid4()
        app = create_service_app(
            service_name="training-management",
            title="test",
            api_version="test",
            settings=ServiceSettings(app_env="test"),
            routers=[
                build_food_intake_history_router(
                    verify_token=VERIFIER.verify,
                    get_intakes=GetFoodIntakeHistory(
                        reader=InMemoryFoodIntakeReader(self.read),
                        authorizations=InMemoryAuthorizationReader(self.read),
                        permissions=PERMISSIONS,
                    ),
                )
            ],
        )
        self.client = TestClient(app)
        self.athlete = {"Authorization": f"Bearer {token(self.athlete_id, 'athlete')}"}
        self.professional = {
            "Authorization": f"Bearer {token(self.professional_id, 'service_provider')}"
        }
        self.athlete_path = f"/v1/athletes/{self.athlete_id}/food-intakes"

    def get(self, path: str, headers: dict[str, str], **params: Any) -> Any:
        return self.client.get(path, headers=headers, params=params)

    def add(self, consumed_at: datetime, meal: MealType = MealType.BREAKFAST) -> str:
        intake = stored_intake(self.athlete_id, consumed_at, meal)
        self.read.food_intakes[intake.intake_id] = intake
        return str(intake.intake_id)

    def test_own_history_shape_order_pagination_and_cache(self) -> None:
        """CA1, CA4 y CA5 por HTTP."""
        empty = self.get(OWN, self.athlete)
        ids = [self.add(NOW - timedelta(hours=hours)) for hours in (5, 1, 3)]

        response = self.get(OWN, self.athlete, limit=2)

        self.assertEqual(empty.status_code, 200)
        self.assertEqual(empty.json(), {"items": [], "next_cursor": None})
        body = response.json()
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual([item["intake_id"] for item in body["items"]], [ids[1], ids[2]])
        self.assertEqual(
            body["items"][0],
            {
                "intake_id": ids[1],
                "athlete_id": str(self.athlete_id),
                "consumed_at": "2026-10-12T11:00:00Z",
                "meal_type": "BREAKFAST",
                "items": [
                    {"food_code": "OATS", "quantity": "80.0", "unit": "G"},
                    {"food_code": "MILK", "quantity": "250.0", "unit": "ML"},
                ],
                "catalog_version": "food-catalog-2026-10-13.v1",
                "version": 1,
                "registered_at": "2026-10-12T11:01:00Z",
            },
        )
        rest = self.get(OWN, self.athlete, limit=2, cursor=body["next_cursor"])
        self.assertEqual([item["intake_id"] for item in rest.json()["items"]], [ids[0]])
        self.assertIsNone(rest.json()["next_cursor"])

    def test_filters_by_meal_type_and_inclusive_period(self) -> None:
        inside = self.add(datetime(2026, 10, 8, 23, 59, tzinfo=UTC), MealType.DINNER)
        self.add(datetime(2026, 10, 9, 0, 0, tzinfo=UTC), MealType.DINNER)
        self.add(datetime(2026, 10, 8, 12, 0, tzinfo=UTC), MealType.LUNCH)

        response = self.get(
            OWN, self.athlete, **{"from": "2026-10-08", "to": "2026-10-08", "meal_type": "DINNER"}
        )

        self.assertEqual([i["intake_id"] for i in response.json()["items"]], [inside])

    def test_professional_route_requires_permission_and_active_authorization(self) -> None:
        """CA3 por HTTP: `403` sin revelar si el deportista existe."""
        self.add(NOW - timedelta(hours=1))
        without = self.get(self.athlete_path, self.professional)
        unknown = self.get(f"/v1/athletes/{uuid4()}/food-intakes", self.professional)
        key = (self.athlete_id, self.professional_id)
        self.read.authorizations[key] = authorization(*key, status="REVOKED")
        revoked = self.get(self.athlete_path, self.professional)
        self.read.authorizations[key] = authorization(*key)
        allowed = self.get(self.athlete_path, self.professional)
        by_athlete = self.get(self.athlete_path, self.athlete)
        own_by_professional = self.get(OWN, self.professional)

        for response in (without, unknown, revoked):
            self.assertEqual(
                (response.status_code, response.json()["code"]),
                (403, "athlete_authorization_required"),
            )
        self.assertEqual(without.json()["message"], unknown.json()["message"])
        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(len(allowed.json()["items"]), 1)
        self.assertEqual(allowed.headers["Cache-Control"], "no-store")
        self.assertEqual(
            (by_athlete.status_code, by_athlete.json()["code"]), (403, "permission_required")
        )
        self.assertEqual(own_by_professional.json()["code"], "permission_required")

    def test_validation_errors(self) -> None:
        """CA2: periodo, cursor, límite y tipo de comida inválidos → `422 validation_error`."""
        cases = (
            ({"from": "2026-10-09", "to": "2026-10-08"}, "query.to"),
            ({"cursor": "basura"}, "query.cursor"),
            ({"limit": 0}, "query.limit"),
            ({"limit": 101}, "query.limit"),
            ({"meal_type": "BRUNCH"}, "query.meal_type"),
            ({"from": "ayer"}, "query.from"),
        )
        for params, field in cases:
            with self.subTest(field=field, params=params):
                response = self.get(OWN, self.athlete, **params)
                self.assertEqual(response.status_code, 422)
                self.assertEqual(response.json()["code"], "validation_error")
                self.assertEqual(response.json()["details"][0]["field"], field)
        key = (self.athlete_id, self.professional_id)
        self.read.authorizations[key] = authorization(*key)
        corrupt = self.get(self.athlete_path, self.professional, cursor="basura")
        self.assertEqual(corrupt.status_code, 422)

    def test_authentication_is_required(self) -> None:
        for path in (OWN, self.athlete_path):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 401)


if __name__ == "__main__":
    unittest.main()
