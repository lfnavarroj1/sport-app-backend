"""API de HU043 en el servicio: `POST /v1/me/food-intakes` (ADR-037), errores y seguridad."""

import unittest
from typing import Any
from uuid import uuid4

from fakes import FixedClock
from fastapi.testclient import TestClient
from intake_fakes import (
    InMemoryFoodIntakeStore,
    InMemoryFoodIntakeUnitOfWork,
    intake_body,
    intake_item,
)
from nutrition_fakes import StaticFoodCatalog
from test_api import VERIFIER, token

from sportapp_platform.app import create_service_app
from sportapp_platform.config import ServiceSettings
from training_management.api.food_intakes import build_food_intakes_router
from training_management.application.commands.register_food_intake import RegisterFoodIntake

INTAKES = "/v1/me/food-intakes"


class FoodIntakesApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = InMemoryFoodIntakeStore()
        self.athlete_id = uuid4()
        app = create_service_app(
            service_name="training-management",
            title="t",
            api_version="t",
            settings=ServiceSettings(app_env="test"),
            routers=[
                build_food_intakes_router(
                    verify_token=VERIFIER.verify,
                    register=RegisterFoodIntake(
                        unit_of_work=lambda: InMemoryFoodIntakeUnitOfWork(self.store),
                        catalog=StaticFoodCatalog(),
                        clock=FixedClock(),
                    ),
                )
            ],
        )
        self.client = TestClient(app)
        self.auth = {"Authorization": f"Bearer {token(self.athlete_id, 'athlete')}"}

    def register(self, body: Any, key: str | None = None, **headers: str) -> Any:
        sent = {**self.auth, **headers}
        if key is not None:
            sent["Idempotency-Key"] = key
        return self.client.post(INTAKES, json=body, headers=sent)

    def test_registers_the_normalized_intake(self) -> None:
        """CA1: `201` con el consumo normalizado, versión 1 y `registered_at`."""
        body = intake_body()

        response = self.register(body, str(uuid4()))

        self.assertEqual(response.status_code, 201, response.text)
        self.assertNotIn("Idempotent-Replayed", response.headers)
        self.assertEqual(
            response.json(),
            {
                "intake_id": body["intake_id"],
                "athlete_id": str(self.athlete_id),
                "consumed_at": "2026-10-12T07:30:00Z",
                "meal_type": "BREAKFAST",
                "items": [
                    {"food_code": "OATS", "quantity": "80.0", "unit": "G"},
                    {"food_code": "BANANA", "quantity": "1.0", "unit": "UNIT"},
                    {"food_code": "MILK", "quantity": "250.0", "unit": "ML"},
                ],
                "catalog_version": "food-catalog-2026-10-13.v1",
                "version": 1,
                "registered_at": "2026-10-12T12:00:00Z",
            },
        )

    def test_replay_existing_and_conflicts(self) -> None:
        """CA3: misma clave → original con `Idempotent-Replayed`; mismo `intake_id` → `201`
        existente; datos distintos → `409 intake_id_reused`; clave reutilizada → `409`."""
        body, key = intake_body(), str(uuid4())
        first = self.register(body, key)

        replay = self.register(body, key)
        existing = self.register(body, str(uuid4()))
        reused_id = self.register({**body, "meal_type": "LUNCH"}, str(uuid4()))
        reused_key = self.register(intake_body(), key)

        self.assertEqual((replay.status_code, replay.headers["Idempotent-Replayed"]), (201, "true"))
        self.assertEqual(replay.json(), first.json())
        self.assertEqual(existing.status_code, 201)
        self.assertNotIn("Idempotent-Replayed", existing.headers)
        self.assertEqual(existing.json(), first.json())
        self.assertEqual(
            (reused_id.status_code, reused_id.json()["code"]), (409, "intake_id_reused")
        )
        self.assertEqual(
            (reused_key.status_code, reused_key.json()["code"]), (409, "idempotency_key_reused")
        )
        self.assertEqual(len(self.store.outbox), 1)

    def test_validation_errors_list_every_rule(self) -> None:
        """CA2: `422 validation_error` con `details[{field, issue}]`; nada se persiste."""
        response = self.register(
            intake_body(
                meal_type="BRUNCH",
                consumed_at="2026-10-12T13:00:00Z",
                items=[intake_item("PIZZA"), intake_item("WATER", "0.0", "G")],
            ),
            "no-uuid",
        )

        self.assertEqual(response.status_code, 422)
        body = response.json()
        self.assertEqual(body["code"], "validation_error")
        self.assertEqual(
            {(item["field"], item["issue"]) for item in body["details"]},
            {
                ("Idempotency-Key", "invalid_uuid"),
                ("meal_type", "unknown_value"),
                ("consumed_at", "too_far_in_future"),
                ("items[0].food_code", "unknown_value"),
                ("items[1].quantity", "out_of_range"),
                ("items[1].unit", "unit_not_allowed"),
            },
        )
        self.assertEqual((self.store.intakes, self.store.outbox), ({}, []))

    def test_athlete_id_in_body_and_invalid_json(self) -> None:
        """Seguridad: el deportista sale del token; `athlete_id` en el cuerpo es `422`."""
        response = self.register(intake_body(athlete_id=str(uuid4())), str(uuid4()))
        self.assertEqual(response.status_code, 422)
        self.assertEqual(
            response.json()["details"], [{"field": "athlete_id", "issue": "field_not_allowed"}]
        )
        for raw in (b"", b"no-json", b"[]"):
            with self.subTest(raw=raw):
                invalid = self.client.post(
                    INTAKES,
                    content=raw,
                    headers={
                        **self.auth,
                        "Idempotency-Key": str(uuid4()),
                        "Content-Type": "application/json",
                    },
                )
                self.assertEqual(invalid.status_code, 422)
        self.assertEqual(self.store.intakes, {})

    def test_other_actors_and_missing_token(self) -> None:
        """Seguridad: otro actor → `403 permission_required`; sin token → `401`."""
        for actor in ("service_provider", "organizer"):
            with self.subTest(actor=actor):
                response = self.client.post(
                    INTAKES,
                    json=intake_body(),
                    headers={
                        "Authorization": f"Bearer {token(uuid4(), actor)}",
                        "Idempotency-Key": str(uuid4()),
                    },
                )
                self.assertEqual(response.status_code, 403)
                self.assertEqual(response.json()["code"], "permission_required")
        anonymous = self.client.post(INTAKES, json=intake_body())
        self.assertEqual(anonymous.status_code, 401)
        self.assertEqual(self.store.intakes, {})


if __name__ == "__main__":
    unittest.main()
