"""Contratos internos `POST /v1/users/lookup` (HU010), `POST /v1/users/lookup-batch` (HU011,
CA5) y `POST /v1/users/directory-lookup` (HU022, ADR-039) con un directorio en memoria."""

import unittest
import uuid

from fastapi.testclient import TestClient

from sportapp_platform.app import create_service_app
from sportapp_platform.config import ServiceSettings
from users_management.api.lookup import build_lookup_router
from users_management.application.ports import UserDisplay, UserReference
from users_management.application.queries.directory_lookup import LookupDirectoryNames
from users_management.application.queries.lookup_user import LookupUserByEmail
from users_management.application.queries.lookup_users_batch import LookupUsersByIds
from users_management.domain.account.model import ActorType


class InMemoryDirectory:
    def __init__(
        self, entries: dict[str, UserReference], displays: dict[uuid.UUID, UserDisplay]
    ) -> None:
        self.entries = entries
        self.displays = displays
        self.queries: list[str] = []
        self.batches: list[tuple[uuid.UUID, ...]] = []

    def find_active_by_email(self, email: str) -> UserReference | None:
        self.queries.append(email)
        return self.entries.get(email)

    def find_active_by_ids(self, user_ids: tuple[uuid.UUID, ...]) -> list[UserDisplay]:
        self.batches.append(user_ids)
        return [self.displays[user_id] for user_id in user_ids if user_id in self.displays]


class LookupApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.athlete = UserReference(uuid.uuid4(), ActorType.ATHLETE)
        self.ana = UserDisplay(uuid.uuid4(), "Ana Pérez", "ana@example.com")
        self.luis = UserDisplay(uuid.uuid4(), "Luis Gómez", "luis@example.com")
        self.directory = InMemoryDirectory(
            {"ana@example.com": self.athlete},
            {self.ana.user_id: self.ana, self.luis.user_id: self.luis},
        )
        app = create_service_app(
            service_name="users-management",
            title="t",
            api_version="0",
            settings=ServiceSettings(app_env="test"),  # type: ignore[call-arg]
            routers=[
                build_lookup_router(
                    LookupUserByEmail(self.directory),
                    LookupUsersByIds(self.directory),
                    LookupDirectoryNames(LookupUsersByIds(self.directory)),
                )
            ],
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

    # --- HU011 (CA5): lote de nombres y correos ---

    def test_batch_returns_found_accounts_in_request_order(self) -> None:
        missing = uuid.uuid4()
        ids = [str(self.luis.user_id), str(missing), str(self.ana.user_id)]

        response = self.client.post("/v1/users/lookup-batch", json={"user_ids": ids})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(
            response.json()["items"],
            [
                {
                    "user_id": str(self.luis.user_id),
                    "full_name": "Luis Gómez",
                    "email": "luis@example.com",
                },
                {
                    "user_id": str(self.ana.user_id),
                    "full_name": "Ana Pérez",
                    "email": "ana@example.com",
                },
            ],
        )
        self.assertEqual(self.directory.batches, [(self.luis.user_id, missing, self.ana.user_id)])

    def test_batch_limits_are_validated(self) -> None:
        cases = {
            "vacío": ([], ("body.user_ids", "too_short")),
            "más de 100": ([str(uuid.uuid4()) for _ in range(101)], ("body.user_ids", "too_long")),
            "repetidos": ([str(self.ana.user_id)] * 2, ("body.user_ids", "duplicate_value")),
            "no UUID": (["no-es-uuid"], ("body.user_ids.0", "uuid_parsing")),
        }
        for name, (ids, (field, issue)) in cases.items():
            with self.subTest(case=name):
                response = self.client.post("/v1/users/lookup-batch", json={"user_ids": ids})

                self.assertEqual(response.status_code, 422)
                self.assertEqual(response.json()["code"], "validation_error")
                self.assertEqual(response.json()["details"], [{"field": field, "issue": issue}])
                self.assertNotIn("no-es-uuid", response.text)
        extra = self.client.post(
            "/v1/users/lookup-batch", json={"user_ids": [str(self.ana.user_id)], "email": "x"}
        )
        self.assertEqual(extra.status_code, 422)
        self.assertEqual(self.directory.batches, [])

    # --- HU022 (ADR-039): directorio de nombres para los inscritos del organizador ---

    def test_directory_lookup_returns_only_names_in_request_order(self) -> None:
        """Identidad mínima: `full_name` sin correo; las cuentas no encontradas se omiten."""
        missing = uuid.uuid4()
        ids = [str(self.luis.user_id), str(missing), str(self.ana.user_id)]

        with self.assertLogs("sportapp.audit", level="INFO") as audit:
            response = self.client.post("/v1/users/directory-lookup", json={"user_ids": ids})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(
            response.json(),
            {
                "items": [
                    {"user_id": str(self.luis.user_id), "full_name": "Luis Gómez"},
                    {"user_id": str(self.ana.user_id), "full_name": "Ana Pérez"},
                ]
            },
        )
        self.assertNotIn("example.com", response.text)
        [record] = [r for r in audit.records if r.getMessage() == "audit.directory_lookup"]
        self.assertEqual(record.fields, {"requested": 3, "found": 2})  # type: ignore[attr-defined]
        rendered = " ".join(audit.output) + str(record.fields)  # type: ignore[attr-defined]
        for value in ("Luis", "Ana", str(self.ana.user_id)):
            self.assertNotIn(value, rendered)

    def test_directory_lookup_limits_are_the_batch_limits(self) -> None:
        cases = {
            "vacío": ([], ("body.user_ids", "too_short")),
            "más de 100": ([str(uuid.uuid4()) for _ in range(101)], ("body.user_ids", "too_long")),
            "repetidos": ([str(self.ana.user_id)] * 2, ("body.user_ids", "duplicate_value")),
            "no UUID": (["no-es-uuid"], ("body.user_ids.0", "uuid_parsing")),
        }
        for name, (ids, (field, issue)) in cases.items():
            with self.subTest(case=name):
                response = self.client.post("/v1/users/directory-lookup", json={"user_ids": ids})

                self.assertEqual(response.status_code, 422)
                self.assertEqual(response.json()["details"], [{"field": field, "issue": issue}])
        hundred = [str(uuid.uuid4()) for _ in range(100)]
        self.assertEqual(
            self.client.post("/v1/users/directory-lookup", json={"user_ids": hundred}).status_code,
            200,
        )


if __name__ == "__main__":
    unittest.main()
