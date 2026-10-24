"""Contrato interno `GET /v1/users/{user_id}/athlete-context` (ADR-041, HU026) con un lector en
memoria: solo deportes, objetivo, ciudad, país y fecha de nacimiento; cuenta sin perfil con los
campos vacíos; auditoría sin valores; un token de cliente se rechaza."""

import unittest
import uuid

from fastapi.testclient import TestClient

from sportapp_platform.app import create_service_app
from sportapp_platform.config import ServiceSettings
from users_management.api.athlete_context import build_athlete_context_router
from users_management.application.errors import ReadModelUnavailable
from users_management.application.ports import AthleteContext
from users_management.application.queries.athlete_context import GetAthleteContext

AUDIT = "audit.athlete_context_read"
CONTEXT = AthleteContext(
    sports=("running", "cycling"),
    primary_goal="PERFORMANCE",
    city="Medellín",
    country_code="CO",
    birth_date="1990-05-14",
)
EMPTY = AthleteContext(sports=(), primary_goal=None, city=None, country_code=None, birth_date=None)


class InMemoryContexts:
    def __init__(self) -> None:
        self.by_user: dict[uuid.UUID, AthleteContext] = {}
        self.unavailable = False
        self.queries: list[uuid.UUID] = []

    def find(self, user_id: uuid.UUID) -> AthleteContext | None:
        self.queries.append(user_id)
        if self.unavailable:
            raise ReadModelUnavailable()
        return self.by_user.get(user_id)


class AthleteContextApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.athlete, self.without_profile = uuid.uuid4(), uuid.uuid4()
        self.reader = InMemoryContexts()
        self.reader.by_user = {self.athlete: CONTEXT, self.without_profile: EMPTY}
        app = create_service_app(
            service_name="users-management",
            title="t",
            api_version="0",
            settings=ServiceSettings(app_env="test"),  # type: ignore[call-arg]
            routers=[build_athlete_context_router(GetAthleteContext(self.reader))],
        )
        self.client = TestClient(app)

    def context(self, user_id: uuid.UUID | str, **headers: str) -> object:
        return self.client.get(f"/v1/users/{user_id}/athlete-context", headers=headers)

    def audited(self, audit: object) -> list[dict[str, object]]:
        return [
            record.fields  # type: ignore[attr-defined]
            for record in audit.records  # type: ignore[attr-defined]
            if record.getMessage() == AUDIT
        ]

    def test_projected_profile_returns_only_the_context(self) -> None:
        """ADR-041: deportes, objetivo, ciudad, país y nacimiento; ninguna otra sección."""
        with self.assertLogs("sportapp.audit", level="INFO") as audit:
            response = self.context(self.athlete)

        self.assertEqual(response.status_code, 200, response.text)  # type: ignore[attr-defined]
        self.assertEqual(
            response.json(),  # type: ignore[attr-defined]
            {
                "sports": ["running", "cycling"],
                "primary_goal": "PERFORMANCE",
                "city": "Medellín",
                "country_code": "CO",
                "birth_date": "1990-05-14",
            },
        )
        self.assertEqual(response.headers["Cache-Control"], "no-store")  # type: ignore[attr-defined]
        self.assertEqual(
            self.audited(audit),
            [{"user_id": str(self.athlete), "consumer": "bff-mobile", "result": "found"}],
        )
        rendered = " ".join(audit.output) + str(self.audited(audit))
        for value in ("Medellín", "1990-05-14", "running", "PERFORMANCE"):
            self.assertNotIn(value, rendered)

    def test_active_account_without_profile_has_empty_context(self) -> None:
        """Sin HU005 ni HU006: `sports` vacío y el resto `null` (el BFF no personaliza)."""
        with self.assertLogs("sportapp.audit", level="INFO") as audit:
            response = self.context(self.without_profile)

        self.assertEqual(response.status_code, 200)  # type: ignore[attr-defined]
        self.assertEqual(
            response.json(),  # type: ignore[attr-defined]
            {
                "sports": [],
                "primary_goal": None,
                "city": None,
                "country_code": None,
                "birth_date": None,
            },
        )
        self.assertEqual([entry["result"] for entry in self.audited(audit)], ["context_missing"])

    def test_unknown_or_disabled_user_is_not_found(self) -> None:
        with self.assertLogs("sportapp.audit", level="INFO") as audit:
            response = self.context(uuid.uuid4())

        self.assertEqual(
            (response.status_code, response.json()["code"]),  # type: ignore[attr-defined]
            (404, "user_not_found"),
        )
        self.assertEqual(response.headers["Cache-Control"], "no-store")  # type: ignore[attr-defined]
        self.assertEqual([entry["result"] for entry in self.audited(audit)], ["user_not_found"])

    def test_a_client_token_is_rejected_without_reading(self) -> None:
        with self.assertLogs("sportapp.audit", level="INFO") as audit:
            response = self.context(self.athlete, Authorization="Bearer token-de-cliente")

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
                    "consumer": "bff-mobile",
                    "result": "rejected",
                    "reason": "authorization_present",
                }
            ],
        )

    def test_read_model_down_is_503(self) -> None:
        self.reader.unavailable = True
        with self.assertLogs("sportapp.audit", level="INFO") as audit:
            response = self.context(self.athlete)

        self.assertEqual(
            (response.status_code, response.json()["code"]),  # type: ignore[attr-defined]
            (503, "upstream_unavailable"),
        )
        self.assertEqual(response.headers["Cache-Control"], "no-store")  # type: ignore[attr-defined]
        self.assertEqual([entry["result"] for entry in self.audited(audit)], ["unavailable"])

    def test_non_uuid_identifier_is_422(self) -> None:
        response = self.context("no-es-uuid")

        self.assertEqual(response.status_code, 422)  # type: ignore[attr-defined]
        self.assertEqual(self.reader.queries, [])

    def test_context_repr_hides_personal_data(self) -> None:
        self.assertNotIn("Medellín", repr(CONTEXT))
        self.assertNotIn("1990", repr(CONTEXT))


if __name__ == "__main__":
    unittest.main()
