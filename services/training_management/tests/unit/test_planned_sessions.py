"""HU035 en Entrenamientos: sesiones planificadas del deportista (ADR-031), sin infraestructura."""

import unittest
from datetime import date
from typing import Any
from uuid import UUID, uuid4

from fakes import FixedClock, InMemoryProjectionUnitOfWork, InMemoryReadStore
from fastapi.testclient import TestClient
from test_api import VERIFIER, token
from test_workers import a_plan, envelope

from sportapp_platform.app import create_service_app
from sportapp_platform.config import ServiceSettings
from training_management.api.planned_sessions import build_planned_sessions_router
from training_management.application.errors import PermissionRequired
from training_management.application.messages import parse_training_event
from training_management.application.ports import (
    Caller,
    PlannedSessionEntry,
    PlannedSessionQuery,
)
from training_management.application.projections.snapshot_projection import TrainingProjector
from training_management.application.queries.planned_sessions import (
    GetPlannedSessions,
    InvalidPlannedSessionQuery,
    PlannedSessionFilters,
)
from training_management.domain.plans.events import planned_session_id


class InMemoryPlannedSessionReader:
    def __init__(self, entries: list[PlannedSessionEntry]) -> None:
        self.entries = entries
        self.queries: list[PlannedSessionQuery] = []

    def list(self, query: PlannedSessionQuery) -> list[PlannedSessionEntry]:
        self.queries.append(query)
        key = lambda e: (e.scheduled_date, e.plan_id, e.session_id)  # noqa: E731
        selected = sorted(
            (
                e
                for e in self.entries
                if e.athlete_id == query.athlete_id
                and query.date_from <= e.scheduled_date <= query.date_to
                and (
                    query.after is None
                    or key(e)
                    > (query.after.scheduled_date, query.after.plan_id, query.after.session_id)
                )
            ),
            key=key,
        )
        return selected[: query.limit]


def entry(athlete_id: UUID, day: date, **overrides: Any) -> PlannedSessionEntry:
    plan_id = overrides.pop("plan_id", uuid4())
    values: dict[str, Any] = {
        "session_id": uuid4(),
        "plan_id": plan_id,
        "athlete_id": athlete_id,
        "plan_title": "Base aeróbica noviembre",
        "scheduled_date": day,
        "sport": "running",
        "duration_minutes": 45,
        "intensity_rpe": 4,
    }
    values.update(overrides)
    return PlannedSessionEntry(**values)


class GetPlannedSessionsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.athlete = Caller(uuid4(), "athlete")
        self.entries = [
            entry(self.athlete.user_id, date(2026, 11, day)) for day in (1, 15, 30, 30)
        ] + [entry(self.athlete.user_id, date(2026, 12, 1)), entry(uuid4(), date(2026, 11, 15))]
        self.reader = InMemoryPlannedSessionReader(self.entries)
        self.query = GetPlannedSessions(reader=self.reader)

    def test_own_sessions_with_inclusive_dates(self) -> None:
        page = self.query.own(
            self.athlete, PlannedSessionFilters(date(2026, 11, 1), date(2026, 11, 30))
        )

        self.assertEqual(len(page.items), 4)
        self.assertTrue(all(item.athlete_id == self.athlete.user_id for item in page.items))
        self.assertEqual(
            [item.scheduled_date.day for item in page.items], [1, 15, 30, 30], "orden estable"
        )
        self.assertIsNone(page.next_cursor)
        self.assertEqual(self.reader.queries[0].athlete_id, self.athlete.user_id)

    def test_keyset_pages_do_not_lose_or_repeat(self) -> None:
        filters = PlannedSessionFilters(date(2026, 11, 1), date(2026, 12, 31), limit=2)
        seen: list[UUID] = []
        cursor: str | None = None
        while True:
            page = self.query.own(
                self.athlete,
                PlannedSessionFilters(filters.date_from, filters.date_to, 2, cursor),
            )
            seen.extend(item.session_id for item in page.items)
            cursor = page.next_cursor
            if cursor is None:
                break

        own = [e for e in self.entries if e.athlete_id == self.athlete.user_id]
        self.assertEqual(sorted(seen), sorted(e.session_id for e in own))
        self.assertEqual(len(seen), len(set(seen)))

    def test_rules(self) -> None:
        with self.assertRaises(PermissionRequired):
            self.query.own(
                Caller(uuid4(), "service_provider"),
                PlannedSessionFilters(date(2026, 11, 1), date(2026, 11, 2)),
            )
        for filters, field, issue in (
            (
                PlannedSessionFilters(date(2026, 11, 2), date(2026, 11, 1)),
                "to",
                "ends_before_starts",
            ),
            (
                PlannedSessionFilters(date(2026, 11, 1), date(2026, 11, 2), 201),
                "limit",
                "out_of_range",
            ),
            (
                PlannedSessionFilters(date(2026, 11, 1), date(2026, 11, 2), 10, "x"),
                "cursor",
                "invalid_cursor",
            ),
        ):
            with self.subTest(issue=issue), self.assertRaises(InvalidPlannedSessionQuery) as raised:
                self.query.own(self.athlete, filters)
            self.assertEqual((raised.exception.field, raised.exception.issue), (field, issue))


class PlannedSessionsApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.athlete_id = uuid4()
        self.entries = [entry(self.athlete_id, date(2026, 11, 16))]
        app = create_service_app(
            service_name="training-management",
            title="t",
            api_version="t",
            settings=ServiceSettings(app_env="test"),
            routers=[
                build_planned_sessions_router(
                    verify_token=VERIFIER.verify,
                    get_sessions=GetPlannedSessions(
                        reader=InMemoryPlannedSessionReader(self.entries)
                    ),
                )
            ],
        )
        self.client = TestClient(app)

    def get(self, query: str, actor: str = "athlete", user: UUID | None = None) -> Any:
        return self.client.get(
            f"/v1/me/training-plan-sessions?{query}",
            headers={"Authorization": f"Bearer {token(user or self.athlete_id, actor)}"},
        )

    def test_contract(self) -> None:
        response = self.get("from=2026-11-01&to=2026-11-30")

        self.assertEqual(response.status_code, 200, response.text)
        [item] = response.json()["items"]
        self.assertEqual(
            set(item),
            {
                "plan_id",
                "plan_title",
                "session_id",
                "scheduled_date",
                "sport",
                "duration_minutes",
                "rpe",
            },
        )
        self.assertEqual((item["scheduled_date"], item["rpe"]), ("2026-11-16", 4))
        self.assertIsNone(response.json()["next_cursor"])

    def test_errors(self) -> None:
        self.assertEqual(self.get("from=2026-11-01&to=2026-11-30", "organizer").status_code, 403)
        inverted = self.get("from=2026-11-30&to=2026-11-01")
        self.assertEqual(
            (inverted.status_code, inverted.json()["details"]),
            (422, [{"field": "query.to", "issue": "ends_before_starts"}]),
        )
        self.assertEqual(self.get("from=ayer&to=2026-11-30").status_code, 422)
        self.assertEqual(self.get("to=2026-11-30").status_code, 422)
        self.assertEqual(
            self.get("from=2026-11-01&to=2026-11-30", user=uuid4()).json()["items"], []
        )


class PlannedSessionProjectionTest(unittest.TestCase):
    def test_plan_created_projects_its_sessions(self) -> None:
        read = InMemoryReadStore()
        project = TrainingProjector(
            unit_of_work=lambda: InMemoryProjectionUnitOfWork(read), clock=FixedClock()
        )
        plan = a_plan()

        project(parse_training_event(envelope(plan)))

        self.assertEqual(read.planned[plan.plan_id].plan_id, plan.plan_id)
        self.assertEqual(planned_session_id(plan.plan_id, 0), planned_session_id(plan.plan_id, 0))
        self.assertNotEqual(
            planned_session_id(plan.plan_id, 0), planned_session_id(plan.plan_id, 1)
        )


if __name__ == "__main__":
    unittest.main()
