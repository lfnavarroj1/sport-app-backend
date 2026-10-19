"""HU011 (CA5): deportistas que autorizaron al profesional, sin infraestructura y por HTTP."""

import unittest
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

from fakes import NOW, InMemoryReadStore
from fastapi.testclient import TestClient
from test_api import VERIFIER, token
from test_history import PERMISSIONS

from sportapp_platform.app import create_service_app
from sportapp_platform.config import ServiceSettings
from training_management.api.authorized_athletes import build_authorized_athletes_router
from training_management.application.errors import PermissionRequired
from training_management.application.ports import (
    AuthorizationView,
    AuthorizedAthleteEntry,
    AuthorizedAthletesQuery,
    Caller,
)
from training_management.application.queries.authorized_athletes import (
    GetAuthorizedAthletes,
    decode_cursor,
)
from training_management.application.queries.training_history import InvalidHistoryQuery


class InMemoryAuthorizedAthleteReader:
    """Mismo filtro y orden que `SqlAlchemyAuthorizedAthleteReader`."""

    def __init__(self, store: InMemoryReadStore) -> None:
        self.store = store
        self.queries: list[AuthorizedAthletesQuery] = []

    def list(self, query: AuthorizedAthletesQuery) -> list[AuthorizedAthleteEntry]:
        self.queries.append(query)
        entries = sorted(
            (
                AuthorizedAthleteEntry(athlete_id=view.athlete_id, authorized_at=view.granted_at)
                for view in self.store.authorizations.values()
                if view.professional_id == query.professional_id and view.is_active
            ),
            key=lambda entry: (entry.authorized_at, entry.athlete_id),
            reverse=True,
        )
        if query.after is not None:
            after = (query.after.authorized_at, query.after.athlete_id)
            entries = [e for e in entries if (e.authorized_at, e.athlete_id) < after]
        return entries[: query.limit]


def grant(
    store: InMemoryReadStore,
    professional_id: UUID,
    *,
    days_ago: int,
    status: str = "ACTIVE",
    athlete_id: UUID | None = None,
) -> UUID:
    athlete_id = athlete_id or uuid4()
    store.authorizations[(athlete_id, professional_id)] = AuthorizationView(
        athlete_id=athlete_id,
        professional_id=professional_id,
        status=status,
        version=1 if status == "ACTIVE" else 2,
        granted_at=NOW - timedelta(days=days_ago),
        revoked_at=None if status == "ACTIVE" else NOW,
    )
    return athlete_id


class GetAuthorizedAthletesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = InMemoryReadStore()
        self.reader = InMemoryAuthorizedAthleteReader(self.store)
        self.query = GetAuthorizedAthletes(reader=self.reader, permissions=PERMISSIONS)
        self.professional = Caller(user_id=uuid4(), actor_type="service_provider")

    def test_only_active_authorizations_of_the_caller_newest_first(self) -> None:
        older = grant(self.store, self.professional.user_id, days_ago=5)
        newer = grant(self.store, self.professional.user_id, days_ago=1)
        grant(self.store, self.professional.user_id, days_ago=2, status="REVOKED")
        grant(self.store, uuid4(), days_ago=0)

        page = self.query(self.professional)

        self.assertEqual([item.athlete_id for item in page.items], [newer, older])
        self.assertIsNone(page.next_cursor)

    def test_keyset_pagination_with_ties_on_authorized_at(self) -> None:
        ids = [grant(self.store, self.professional.user_id, days_ago=3) for _ in range(3)]
        ids.append(grant(self.store, self.professional.user_id, days_ago=1))
        expected = [ids[3], *sorted(ids[:3], reverse=True)]

        first = self.query(self.professional, limit=2)
        assert first.next_cursor is not None
        second = self.query(self.professional, limit=2, cursor=first.next_cursor)

        self.assertEqual(self.reader.queries[0].limit, 3)
        self.assertEqual([item.athlete_id for item in (*first.items, *second.items)], expected)
        self.assertIsNone(second.next_cursor)
        position = decode_cursor(first.next_cursor)
        self.assertEqual(position.athlete_id, expected[1])

    def test_permission_and_validation(self) -> None:
        for actor in ("athlete", "organizer"):
            with self.subTest(actor=actor), self.assertRaises(PermissionRequired):
                self.query(Caller(user_id=uuid4(), actor_type=actor))
        for limit in (0, 101):
            with self.subTest(limit=limit), self.assertRaises(InvalidHistoryQuery) as raised:
                self.query(self.professional, limit=limit)
            self.assertEqual(
                (raised.exception.field, raised.exception.issue), ("limit", "out_of_range")
            )
        for cursor in ("no-es-cursor", "e30", "eyJhIjoiMjAyNi0xMC0xMCIsImkiOiJ4In0"):
            with self.subTest(cursor=cursor), self.assertRaises(InvalidHistoryQuery) as raised:
                self.query(self.professional, cursor=cursor)
            self.assertEqual(raised.exception.issue, "invalid_cursor")
        self.assertEqual(self.reader.queries, [])


class AuthorizedAthletesApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = InMemoryReadStore()
        self.professional_id = uuid4()
        app = create_service_app(
            service_name="training-management",
            title="test",
            api_version="test",
            settings=ServiceSettings(app_env="test"),
            routers=[
                build_authorized_athletes_router(
                    verify_token=VERIFIER.verify,
                    get_authorized_athletes=GetAuthorizedAthletes(
                        reader=InMemoryAuthorizedAthleteReader(self.store),
                        permissions=PERMISSIONS,
                    ),
                )
            ],
        )
        self.client = TestClient(app)

    def get(self, actor: str | None = "service_provider", **params: Any) -> Any:
        headers = {"Authorization": f"Bearer {token(self.professional_id, actor)}"} if actor else {}
        return self.client.get("/v1/me/authorized-athletes", headers=headers, params=params)

    def test_shape_order_cursor_and_cache(self) -> None:
        first = grant(self.store, self.professional_id, days_ago=1)
        second = grant(self.store, self.professional_id, days_ago=2)

        response = self.get(limit=1)
        rest = self.get(limit=1, cursor=response.json()["next_cursor"])

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        [item] = response.json()["items"]
        self.assertEqual(set(item), {"athlete_id", "authorized_at"})
        self.assertEqual(item["athlete_id"], str(first))
        self.assertTrue(item["authorized_at"].endswith("Z"))
        self.assertEqual([i["athlete_id"] for i in rest.json()["items"]], [str(second)])
        self.assertIsNone(rest.json()["next_cursor"])

    def test_empty_page(self) -> None:
        response = self.get()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"items": [], "next_cursor": None})

    def test_errors(self) -> None:
        athlete = self.get("athlete")
        cursor = self.get(cursor="no-es-cursor")
        limit = self.get(limit=101)
        anonymous = self.get(None)

        self.assertEqual(
            (athlete.status_code, athlete.json()["code"]), (403, "permission_required")
        )
        self.assertEqual(cursor.status_code, 422)
        self.assertEqual(
            cursor.json()["details"], [{"field": "query.cursor", "issue": "invalid_cursor"}]
        )
        self.assertEqual(limit.status_code, 422)
        self.assertEqual(anonymous.status_code, 401)


if __name__ == "__main__":
    unittest.main()
