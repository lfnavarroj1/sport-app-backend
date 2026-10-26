"""Contrato interno `GET /v1/users/{user_id}/nutrition-restrictions` (ADR-035, HU009 y HU014)
con un lector en memoria: solo patrón, restricciones, alergias y versión; «sin hábitos» como
listas vacías y `null`; auditoría sin valores; un token de cliente se rechaza."""

import json
import logging
import unittest
import uuid

from fastapi.testclient import TestClient

from sportapp_platform.app import create_service_app
from sportapp_platform.config import ServiceSettings
from sportapp_platform.correlation import CORRELATION_HEADER
from sportapp_platform.logging import JsonFormatter
from users_management.api.nutrition_restrictions import build_nutrition_restrictions_router
from users_management.application.errors import ReadModelUnavailable
from users_management.application.ports import NutritionRestrictions
from users_management.application.queries.nutrition_restrictions import (
    GetNutritionRestrictions,
)
from users_management.infrastructure.persistence_read.nutrition_restrictions import NO_HABITS

AUDIT = "audit.nutrition_restrictions_read"
HABITS = NutritionRestrictions(
    restrictions=("GLUTEN_FREE", "LOW_SUGAR"),
    allergies=("PEANUTS", "SESAME"),
    diet_pattern="VEGETARIAN",
    version=2,
)


class InMemoryRestrictions:
    def __init__(self) -> None:
        self.by_user: dict[uuid.UUID, NutritionRestrictions] = {}
        self.unavailable = False
        self.queries: list[uuid.UUID] = []

    def find(self, user_id: uuid.UUID) -> NutritionRestrictions | None:
        self.queries.append(user_id)
        if self.unavailable:
            raise ReadModelUnavailable()
        return self.by_user.get(user_id)


class NutritionRestrictionsApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.athlete, self.without_habits = uuid.uuid4(), uuid.uuid4()
        self.reader = InMemoryRestrictions()
        self.reader.by_user = {self.athlete: HABITS, self.without_habits: NO_HABITS}
        app = create_service_app(
            service_name="users-management",
            title="t",
            api_version="0",
            settings=ServiceSettings(app_env="test"),  # type: ignore[call-arg]
            routers=[build_nutrition_restrictions_router(GetNutritionRestrictions(self.reader))],
        )
        self.client = TestClient(app)

    def restrictions(self, user_id: uuid.UUID, **headers: str) -> object:
        return self.client.get(f"/v1/users/{user_id}/nutrition-restrictions", headers=headers)

    def audited(self, audit: object) -> list[dict[str, object]]:
        return [
            record.fields  # type: ignore[attr-defined]
            for record in audit.records  # type: ignore[attr-defined]
            if record.getMessage() == AUDIT
        ]

    def test_registered_habits_return_only_the_restrictions(self) -> None:
        """HU009 CA3: patrón, restricciones, alergias y versión; ninguna otra sección."""
        correlation = str(uuid.uuid4())
        with self.assertLogs("sportapp.audit", level="INFO") as audit:
            response = self.restrictions(self.athlete, **{CORRELATION_HEADER: correlation})

        self.assertEqual(response.status_code, 200, response.text)  # type: ignore[attr-defined]
        self.assertEqual(
            response.json(),  # type: ignore[attr-defined]
            {
                "restrictions": ["GLUTEN_FREE", "LOW_SUGAR"],
                "allergies": ["PEANUTS", "SESAME"],
                "diet_pattern": "VEGETARIAN",
                "version": 2,
            },
        )
        self.assertEqual(response.headers["Cache-Control"], "no-store")  # type: ignore[attr-defined]
        [record] = [r for r in audit.records if r.getMessage() == AUDIT]
        self.assertEqual(
            record.fields,  # type: ignore[attr-defined]
            {"user_id": str(self.athlete), "consumer": "training-management", "result": "found"},
        )
        rendered = " ".join(audit.output) + str(record.fields)  # type: ignore[attr-defined]
        for value in ("GLUTEN_FREE", "PEANUTS", "VEGETARIAN"):
            self.assertNotIn(value, rendered)

    def test_active_user_without_habits(self) -> None:
        """«Sin hábitos» (contrato 0.8.0, decisión del 2026-10-24 frente a HU009 CA5)."""
        with self.assertLogs("sportapp.audit", level="INFO") as audit:
            response = self.restrictions(self.without_habits)

        self.assertEqual(response.status_code, 200)  # type: ignore[attr-defined]
        self.assertEqual(
            response.json(),  # type: ignore[attr-defined]
            {"restrictions": [], "allergies": [], "diet_pattern": None, "version": None},
        )
        self.assertEqual([entry["result"] for entry in self.audited(audit)], ["habits_missing"])

    def test_unknown_or_disabled_user_is_not_found(self) -> None:
        with self.assertLogs("sportapp.audit", level="INFO") as audit:
            response = self.restrictions(uuid.uuid4())

        self.assertEqual(
            (response.status_code, response.json()["code"]),  # type: ignore[attr-defined]
            (404, "user_not_found"),
        )
        self.assertEqual(response.headers["Cache-Control"], "no-store")  # type: ignore[attr-defined]
        self.assertEqual([entry["result"] for entry in self.audited(audit)], ["user_not_found"])

    def test_a_client_token_is_rejected_without_reading(self) -> None:
        """Seguridad (HU009): si la ruta llegara a exponerse, el token de un cliente → `403`."""
        with self.assertLogs("sportapp.audit", level="INFO") as audit:
            response = self.restrictions(self.athlete, Authorization="Bearer token-de-cliente")

        self.assertEqual(
            (response.status_code, response.json()["code"]),  # type: ignore[attr-defined]
            (403, "permission_required"),
        )
        self.assertEqual(self.reader.queries, [])
        self.assertEqual(
            self.audited(audit),
            [
                {
                    "user_id": str(self.athlete),
                    "consumer": "training-management",
                    "result": "rejected",
                    "reason": "authorization_present",
                }
            ],
        )

    def test_read_model_down_is_503(self) -> None:
        self.reader.unavailable = True
        with self.assertLogs("sportapp.audit", level="INFO") as audit:
            response = self.restrictions(self.athlete)

        self.assertEqual(
            (response.status_code, response.json()["code"]),  # type: ignore[attr-defined]
            (503, "upstream_unavailable"),
        )
        self.assertEqual([entry["result"] for entry in self.audited(audit)], ["unavailable"])

    def test_non_uuid_identifier_is_422(self) -> None:
        response = self.client.get("/v1/users/no-es-uuid/nutrition-restrictions")

        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.reader.queries, [])

    def test_audit_record_carries_the_correlation(self) -> None:
        """La auditoría incluye la correlación de Entrenamientos (formato JSON del log)."""
        correlation = str(uuid.uuid4())
        lines: list[str] = []
        formatter = JsonFormatter(service="users-management", environment="test", version="0")

        class Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                # Se formatea al emitir: la correlación vive en el contexto de la solicitud.
                lines.append(formatter.format(record))

        handler = Capture()
        audit_logger = logging.getLogger("sportapp.audit")
        audit_logger.addHandler(handler)
        self.addCleanup(audit_logger.removeHandler, handler)

        self.restrictions(self.athlete, **{CORRELATION_HEADER: correlation})

        [line] = [entry for entry in lines if AUDIT in entry]
        self.assertEqual(json.loads(line)["correlation_id"], correlation)


if __name__ == "__main__":
    unittest.main()
