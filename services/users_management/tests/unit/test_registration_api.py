"""Contrato HTTP de `POST /v1/users` con el caso de uso real y persistencia en memoria."""

import unittest
import uuid
from typing import Any

from fakes import CURRENT_POLICIES, InMemoryStore, register_user_with
from fastapi.testclient import TestClient

from sportapp_platform.app import create_service_app
from sportapp_platform.config import ServiceSettings
from sportapp_platform.correlation import CORRELATION_HEADER
from users_management.api.registration import build_registration_router

PASSWORD = "contraseña-muy-secreta"


def body(**overrides: Any) -> dict[str, Any]:
    values = {
        "full_name": "Ana Pérez",
        "email": "ana@example.com",
        "password": PASSWORD,
        "actor_type": "athlete",
        "accepted_policies_version": CURRENT_POLICIES,
    }
    values.update(overrides)
    return values


class RegistrationApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = InMemoryStore()
        app = create_service_app(
            service_name="users-management",
            title="t",
            api_version="0",
            settings=ServiceSettings(app_env="test"),  # type: ignore[call-arg]
            routers=[build_registration_router(register_user_with(self.store))],
        )
        self.client = TestClient(app)

    def post(self, payload: dict[str, Any], key: str | None = None) -> Any:
        headers = {"Idempotency-Key": key or str(uuid.uuid4())}
        return self.client.post("/v1/users", json=payload, headers=headers)

    def test_201_with_location_and_confirmed_representation(self) -> None:
        response = self.post(body(email="  Ana@Example.com"))

        data = response.json()
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.headers["Location"], f"/v1/users/{data['user_id']}")
        self.assertNotIn("Idempotent-Replayed", response.headers)
        self.assertEqual(data["email"], "ana@example.com")
        self.assertEqual(data["status"], "active")
        self.assertEqual(data["aggregate_version"], 1)
        self.assertEqual(
            set(data),
            {
                "user_id",
                "full_name",
                "email",
                "actor_type",
                "status",
                "aggregate_version",
                "created_at",
            },
        )

    def test_retry_returns_same_result_marked_as_replayed(self) -> None:
        key = str(uuid.uuid4())
        first = self.post(body(), key)

        second = self.post(body(), key)

        self.assertEqual(second.status_code, 201)
        self.assertEqual(second.headers["Idempotent-Replayed"], "true")
        self.assertEqual(second.json(), first.json())
        self.assertEqual(len(self.store.accounts), 1)

    def test_duplicate_email_returns_409_stable_code(self) -> None:
        self.post(body())

        response = self.post(body(email="ANA@example.com"))

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "email_already_registered")
        self.assertEqual(response.json()["correlation_id"], response.headers[CORRELATION_HEADER])

    def test_reused_key_with_other_body_returns_409(self) -> None:
        key = str(uuid.uuid4())
        self.post(body(), key)

        response = self.post(body(email="otra@example.com"), key)

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "idempotency_key_reused")

    def test_missing_or_invalid_idempotency_key_is_422(self) -> None:
        missing = self.client.post("/v1/users", json=body())
        invalid = self.post(body(), key="no-es-uuid")

        for response in (missing, invalid):
            self.assertEqual(response.status_code, 422)
            self.assertEqual(response.json()["details"][0]["field"], "header.Idempotency-Key")
        self.assertEqual(self.store.accounts, {})

    def test_invalid_fields_are_identified_without_echoing_values(self) -> None:
        response = self.post(
            body(password="corta", email="no-es-correo", actor_type="admin", full_name="")
        )

        fields = {detail["field"] for detail in response.json()["details"]}
        self.assertEqual(response.status_code, 422)
        self.assertEqual(
            fields, {"body.password", "body.email", "body.actor_type", "body.full_name"}
        )
        self.assertNotIn("corta", response.text)
        self.assertEqual(self.store.accounts, {})

    def test_unknown_fields_are_rejected(self) -> None:
        response = self.post(body(role="administrator"))

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["details"][0]["field"], "body.role")

    def test_outdated_policies_return_422_with_stable_code(self) -> None:
        response = self.post(body(accepted_policies_version="2020-01"))

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["code"], "policies_version_mismatch")
        self.assertEqual(response.json()["details"][0]["field"], "body.accepted_policies_version")

    def test_domain_validation_after_transport_returns_422(self) -> None:
        # El separador \x1c pasa el recorte del transporte, pero el dominio lo trata como espacio.
        response = self.post(body(full_name="\x1c"))

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["details"][0]["field"], "body.full_name")

    def test_response_never_contains_password(self) -> None:
        response = self.post(body())

        self.assertNotIn(PASSWORD, response.text)
        self.assertNotIn("password", response.json())


if __name__ == "__main__":
    unittest.main()
