"""HU010: contrato interno `POST /v1/users/lookup` con un directorio en memoria."""

import unittest
import uuid

from fastapi.testclient import TestClient

from sportapp_platform.app import create_service_app
from sportapp_platform.config import ServiceSettings
from users_management.api.lookup import build_lookup_router
from users_management.application.ports import UserReference
from users_management.application.queries.lookup_user import LookupUserByEmail
from users_management.domain.account.model import ActorType


class InMemoryDirectory:
    def __init__(self, entries: dict[str, UserReference]) -> None:
        self.entries = entries
        self.queries: list[str] = []

    def find_active_by_email(self, email: str) -> UserReference | None:
        self.queries.append(email)
        return self.entries.get(email)


class LookupApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.athlete = UserReference(uuid.uuid4(), ActorType.ATHLETE)
        self.directory = InMemoryDirectory({"ana@example.com": self.athlete})
        app = create_service_app(
            service_name="users-management",
            title="t",
            api_version="0",
            settings=ServiceSettings(app_env="test"),  # type: ignore[call-arg]
            routers=[build_lookup_router(LookupUserByEmail(self.directory))],
        )
        self.client = TestClient(app)

    def test_exact_normalized_email_resolves_identity_only(self) -> None:
        response = self.client.post("/v1/users/lookup", json={"email": "  Ana@Example.COM "})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(), {"user_id": str(self.athlete.user_id), "actor_type": "athlete"}
        )
        self.assertEqual(self.directory.queries, ["ana@example.com"])

    def test_unknown_email_is_404(self) -> None:
        response = self.client.post("/v1/users/lookup", json={"email": "otra@example.com"})

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "user_not_found")
        self.assertNotIn("otra@example.com", response.text)

    def test_invalid_body_is_422_without_echo(self) -> None:
        for body in ({"email": "no-es-correo"}, {}, {"email": "a@b.co", "extra": 1}):
            with self.subTest(body=body):
                response = self.client.post("/v1/users/lookup", json=body)
                self.assertEqual(response.status_code, 422)
                self.assertNotIn("no-es-correo", response.text)


if __name__ == "__main__":
    unittest.main()
