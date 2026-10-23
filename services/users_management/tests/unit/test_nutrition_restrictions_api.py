"""Contrato interno `GET /v1/users/{user_id}/nutrition-restrictions` (ADR-035, HU014) con un
directorio en memoria: sin hábitos registrados (HU009) responde listas vacías y `null`."""

import unittest
import uuid

from fastapi.testclient import TestClient

from sportapp_platform.app import create_service_app
from sportapp_platform.config import ServiceSettings
from users_management.api.nutrition_restrictions import build_nutrition_restrictions_router
from users_management.application.queries.nutrition_restrictions import (
    GetNutritionRestrictions,
)


class ActiveUsers:
    def __init__(self, *active: uuid.UUID) -> None:
        self.active = set(active)
        self.queries: list[uuid.UUID] = []

    def is_active(self, user_id: uuid.UUID) -> bool:
        self.queries.append(user_id)
        return user_id in self.active


class NutritionRestrictionsApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.athlete = uuid.uuid4()
        self.users = ActiveUsers(self.athlete)
        app = create_service_app(
            service_name="users-management",
            title="t",
            api_version="0",
            settings=ServiceSettings(app_env="test"),  # type: ignore[call-arg]
            routers=[build_nutrition_restrictions_router(GetNutritionRestrictions(self.users))],
        )
        self.client = TestClient(app)

    def test_active_user_without_habits(self) -> None:
        with self.assertLogs("sportapp.audit", level="INFO") as audit:
            response = self.client.get(f"/v1/users/{self.athlete}/nutrition-restrictions")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            response.json(),
            {"restrictions": [], "allergies": [], "diet_pattern": None, "version": None},
        )
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        [record] = [
            r for r in audit.records if r.getMessage() == "audit.nutrition_restrictions_read"
        ]
        self.assertEqual(
            record.fields,  # type: ignore[attr-defined]
            {"user_id": str(self.athlete), "result": "habits_missing"},
        )

    def test_unknown_or_disabled_user_is_not_found(self) -> None:
        with self.assertLogs("sportapp.audit", level="INFO") as audit:
            response = self.client.get(f"/v1/users/{uuid.uuid4()}/nutrition-restrictions")

        self.assertEqual((response.status_code, response.json()["code"]), (404, "user_not_found"))
        self.assertEqual(audit.records[0].fields["result"], "user_not_found")  # type: ignore[attr-defined]

    def test_identifier_must_be_uuid(self) -> None:
        response = self.client.get("/v1/users/no-es-uuid/nutrition-restrictions")

        self.assertEqual((response.status_code, response.json()["code"]), (422, "validation_error"))
        self.assertEqual(self.users.queries, [])


if __name__ == "__main__":
    unittest.main()
