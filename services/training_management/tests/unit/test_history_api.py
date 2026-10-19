"""API de HU011 en el servicio: rutas del historial con dobles en memoria y tokens firmados."""

import unittest
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from fakes import (
    NOW,
    InMemoryAuthorizationReader,
    InMemoryHistoryReader,
    InMemoryReadStore,
)
from fastapi.testclient import TestClient
from test_api import VERIFIER, token
from test_history import PERMISSIONS, entry

from sportapp_platform.app import create_service_app
from sportapp_platform.config import ServiceSettings
from training_management.api.history import build_history_router
from training_management.application.ports import AuthorizationView
from training_management.application.queries.training_history import GetTrainingHistory


class TrainingHistoryApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.read = InMemoryReadStore()
        self.athlete_id, self.professional_id = uuid4(), uuid4()
        app = create_service_app(
            service_name="training-management",
            title="test",
            api_version="test",
            settings=ServiceSettings(app_env="test"),
            routers=[
                build_history_router(
                    verify_token=VERIFIER.verify,
                    get_history=GetTrainingHistory(
                        reader=InMemoryHistoryReader(self.read),
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

    def get(self, path: str, headers: dict[str, str], **params: Any) -> Any:
        return self.client.get(path, headers=headers, params=params)

    def test_own_history_shape_order_and_cache(self) -> None:
        """CA1 y CA2 por HTTP."""
        empty = self.get("/v1/training-history", self.athlete)
        for days in (3, 1, 2):
            item = entry(self.athlete_id, NOW - timedelta(days=days))
            self.read.sessions[item.session_id] = item

        response = self.get("/v1/training-history", self.athlete, limit=2)

        self.assertEqual(empty.status_code, 200)
        self.assertEqual(empty.json()["items"], [])
        self.assertEqual(empty.json()["next_cursor"], None)
        self.assertIn("running", empty.json()["filters"]["sports"])
        body = response.json()
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        ended = [datetime.fromisoformat(i["ended_at"]) for i in body["items"]]
        self.assertEqual(ended, sorted(ended, reverse=True))
        self.assertEqual(len(body["items"]), 2)
        self.assertIsNotNone(body["next_cursor"])
        self.assertEqual(
            set(body["items"][0]),
            {"session_id", "plan_id", "sport", "started_at", "ended_at", "duration_seconds"},
        )

        rest = self.get("/v1/training-history", self.athlete, limit=2, cursor=body["next_cursor"])
        self.assertEqual(len(rest.json()["items"]), 1)
        self.assertIsNone(rest.json()["next_cursor"])

    def test_professional_route_requires_active_authorization(self) -> None:
        """CA3 por HTTP."""
        path = f"/v1/athletes/{self.athlete_id}/training-history"
        without = self.get(path, self.professional)
        self.read.authorizations[(self.athlete_id, self.professional_id)] = AuthorizationView(
            athlete_id=self.athlete_id,
            professional_id=self.professional_id,
            status="ACTIVE",
            version=1,
            granted_at=NOW,
            revoked_at=None,
        )
        allowed = self.get(path, self.professional)
        by_athlete = self.get(path, self.athlete)
        own_by_professional = self.get("/v1/training-history", self.professional)

        self.assertEqual(
            (without.status_code, without.json()["code"]), (403, "athlete_authorization_required")
        )
        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(by_athlete.json()["code"], "permission_required")
        self.assertEqual(own_by_professional.json()["code"], "permission_required")

    def test_validation_errors(self) -> None:
        cases = (
            ({"from": "2026-10-09", "to": "2026-10-08"}, "query.to"),
            ({"cursor": "basura"}, "query.cursor"),
            ({"limit": 0}, "query.limit"),
            ({"sport": "golf"}, "query.sport"),
            ({"from": "ayer"}, "query.from"),
        )
        for params, field in cases:
            with self.subTest(field=field):
                response = self.get("/v1/training-history", self.athlete, **params)
                self.assertEqual(response.status_code, 422)
                self.assertEqual(response.json()["details"][0]["field"], field)

    def test_authentication_is_required(self) -> None:
        for path in ("/v1/training-history", f"/v1/athletes/{self.athlete_id}/training-history"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 401)

    def test_period_filter_is_inclusive_by_end_date(self) -> None:
        inside = entry(self.athlete_id, datetime(2026, 10, 8, 23, 59, tzinfo=UTC))
        outside = entry(self.athlete_id, datetime(2026, 10, 9, 0, 0, tzinfo=UTC))
        for item in (inside, outside):
            self.read.sessions[item.session_id] = item

        response = self.get(
            "/v1/training-history", self.athlete, **{"from": "2026-10-08", "to": "2026-10-08"}
        )

        self.assertEqual(
            [i["session_id"] for i in response.json()["items"]], [str(inside.session_id)]
        )


if __name__ == "__main__":
    unittest.main()
