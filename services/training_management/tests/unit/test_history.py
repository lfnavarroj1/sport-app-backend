"""HU011 sin infraestructura: eventos de autorización, mensajes, proyecciones y consulta."""

import unittest
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from fakes import (
    NOW,
    FakeCapabilities,
    FakePermissions,
    FixedClock,
    InMemoryAuthorizationReader,
    InMemoryAuthorizationUnitOfWork,
    InMemoryHistoryReader,
    InMemoryProjectionUnitOfWork,
    InMemoryReadStore,
    InMemoryTrainingStore,
)

from training_management.application.commands.authorizations import (
    GrantAuthorization,
    RevokeAuthorization,
)
from training_management.application.errors import (
    AthleteAuthorizationRequired,
    PermissionRequired,
)
from training_management.application.messages import (
    AuthorizationChangedEvent,
    InvalidEvent,
    SessionFinishedEvent,
    parse_training_event,
)
from training_management.application.ports import (
    AuthorizationView,
    Caller,
    SessionHistoryEntry,
)
from training_management.application.projections.plan_projection import ProjectionOutcome
from training_management.application.projections.snapshot_projection import TrainingProjector
from training_management.application.queries.training_history import (
    GetTrainingHistory,
    HistoryFilters,
    InvalidHistoryQuery,
    decode_cursor,
)
from training_management.domain.authorizations.events import (
    GRANTED,
    REVOKED,
    authorization_aggregate_id,
)
from training_management.domain.plans.model import Sport

MANAGE = "training.plan.manage"
HISTORY = "training.history.view_assigned"
PERMISSIONS = FakePermissions(
    {"service_provider": {"training.plan.author", HISTORY}, "organizer": set()}
)


def authorization_envelope(
    athlete_id: UUID, professional_id: UUID, *, version: int = 1, revoked: bool = False
) -> dict[str, Any]:
    return {
        "event_id": str(uuid4()),
        "event_type": REVOKED if revoked else GRANTED,
        "occurred_at": NOW.isoformat(),
        "producer": "training-management",
        "aggregate_type": "training_authorization",
        "aggregate_id": str(authorization_aggregate_id(athlete_id, professional_id)),
        "aggregate_version": version,
        "correlation_id": None,
        "causation_id": None,
        "schema_version": 1,
        "payload": {
            "athlete_id": str(athlete_id),
            "professional_id": str(professional_id),
            "status": "REVOKED" if revoked else "ACTIVE",
            "granted_at": NOW.isoformat(),
            "revoked_at": NOW.isoformat() if revoked else None,
        },
    }


def session_envelope(athlete_id: UUID, **payload: Any) -> dict[str, Any]:
    session_id = payload.pop("session_id", str(uuid4()))
    ended_at = payload.pop("ended_at", NOW)
    body: dict[str, Any] = {
        "session_id": session_id,
        "athlete_id": str(athlete_id),
        "plan_id": None,
        "sport": "running",
        "started_at": (ended_at - timedelta(minutes=45)).isoformat(),
        "ended_at": ended_at.isoformat(),
        "duration_seconds": 2700,
        "status": "FINISHED",
    }
    body.update(payload)
    return {
        "event_id": str(uuid4()),
        "event_type": "entrenamiento.finalizado.v1",
        "occurred_at": NOW.isoformat(),
        "producer": "training-management",
        "aggregate_type": "training_session",
        "aggregate_id": session_id,
        "aggregate_version": 2,
        "correlation_id": str(uuid4()),
        "causation_id": None,
        "schema_version": 1,
        "payload": body,
    }


def entry(athlete_id: UUID, ended_at: datetime, sport: str = "running") -> SessionHistoryEntry:
    return SessionHistoryEntry(
        session_id=uuid4(),
        athlete_id=athlete_id,
        plan_id=None,
        sport=sport,
        started_at=ended_at - timedelta(hours=1),
        ended_at=ended_at,
        duration_seconds=3600,
        version=2,
    )


class AuthorizationEventsTest(unittest.TestCase):
    """HU011: cada cambio real de la autorización deja su evento en la misma transacción."""

    def setUp(self) -> None:
        self.store = InMemoryTrainingStore()
        self.athlete = Caller(uuid4(), "athlete")
        self.professional_id = uuid4()
        clock = FixedClock()
        self.grant = GrantAuthorization(
            unit_of_work=lambda: InMemoryAuthorizationUnitOfWork(self.store),
            capabilities=FakeCapabilities({self.athlete.user_id: {MANAGE}}),
            clock=clock,
        )
        self.revoke = RevokeAuthorization(
            unit_of_work=lambda: InMemoryAuthorizationUnitOfWork(self.store), clock=clock
        )

    def test_grant_revoke_and_reactivate_emit_versioned_snapshots(self) -> None:
        correlation = uuid4()
        self.grant(self.athlete, self.professional_id, correlation)
        self.grant(self.athlete, self.professional_id)  # ya activa: sin evento
        self.revoke(self.athlete, self.professional_id)
        self.revoke(self.athlete, self.professional_id)  # ya revocada: sin evento
        self.grant(self.athlete, self.professional_id)

        events = self.store.outbox
        self.assertEqual([e.event_type for e in events], [GRANTED, REVOKED, GRANTED])
        self.assertEqual([e.aggregate_version for e in events], [1, 2, 3])
        expected_id = authorization_aggregate_id(self.athlete.user_id, self.professional_id)
        self.assertEqual({e.aggregate_id for e in events}, {expected_id})
        self.assertEqual(events[0].correlation_id, correlation)
        self.assertEqual(events[1].payload["status"], "REVOKED")
        self.assertIsNotNone(events[1].payload["revoked_at"])
        self.assertIsNone(events[2].payload["revoked_at"])

    def test_aggregate_id_is_deterministic_per_pair(self) -> None:
        athlete, professional = uuid4(), uuid4()

        self.assertEqual(
            authorization_aggregate_id(athlete, professional),
            authorization_aggregate_id(athlete, professional),
        )
        self.assertNotEqual(
            authorization_aggregate_id(athlete, professional),
            authorization_aggregate_id(professional, athlete),
        )


class ParseHu011EventsTest(unittest.TestCase):
    def test_authorization_events(self) -> None:
        athlete, professional = uuid4(), uuid4()

        granted = parse_training_event(authorization_envelope(athlete, professional))
        revoked = parse_training_event(
            authorization_envelope(athlete, professional, version=2, revoked=True)
        )

        assert isinstance(granted, AuthorizationChangedEvent)
        assert isinstance(revoked, AuthorizationChangedEvent)
        self.assertTrue(granted.authorization.is_active)
        self.assertEqual(
            (revoked.authorization.status, revoked.authorization.version), ("REVOKED", 2)
        )

    def test_authorization_contract_violations(self) -> None:
        athlete, professional = uuid4(), uuid4()
        base = authorization_envelope(athlete, professional)
        cases: dict[str, dict[str, Any]] = {
            "aggregate_id_mismatch": {**base, "aggregate_id": str(uuid4())},
            "status_mismatch": {**base, "payload": {**base["payload"], "status": "REVOKED"}},
            "revoked_at_mismatch": {
                **base,
                "payload": {**base["payload"], "revoked_at": NOW.isoformat()},
            },
            "granted_at_without_timezone": {
                **base,
                "payload": {**base["payload"], "granted_at": "2026-10-12T10:00:00"},
            },
            "unsupported_schema_version": {**base, "schema_version": 2},
        }
        for reason, body in cases.items():
            with self.subTest(reason=reason), self.assertRaises(InvalidEvent) as raised:
                parse_training_event(body)
            self.assertEqual(raised.exception.reason, reason)

    def test_session_finished(self) -> None:
        athlete, plan = uuid4(), uuid4()

        event = parse_training_event(session_envelope(athlete, plan_id=str(plan), sport="cycling"))

        assert isinstance(event, SessionFinishedEvent)
        self.assertEqual((event.session.plan_id, event.session.sport), (plan, "cycling"))
        self.assertEqual(event.session.version, 2)

    def test_session_contract_violations(self) -> None:
        athlete = uuid4()
        base = session_envelope(athlete)
        cases: dict[str, dict[str, Any]] = {
            "aggregate_id_mismatch": {**base, "aggregate_id": str(uuid4())},
            "unknown_status": {**base, "payload": {**base["payload"], "status": "ACTIVE"}},
            "unknown_sport": {**base, "payload": {**base["payload"], "sport": "golf"}},
            "ended_before_started": {
                **base,
                "payload": {**base["payload"], "ended_at": (NOW - timedelta(days=1)).isoformat()},
            },
            "duration_seconds_invalid": {
                **base,
                "payload": {**base["payload"], "duration_seconds": -1},
            },
            "plan_id_not_uuid": {**base, "payload": {**base["payload"], "plan_id": "x"}},
        }
        for reason, body in cases.items():
            with self.subTest(reason=reason), self.assertRaises(InvalidEvent) as raised:
                parse_training_event(body)
            self.assertEqual(raised.exception.reason, reason)


class SnapshotProjectionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.read = InMemoryReadStore()
        self.project = TrainingProjector(
            unit_of_work=lambda: InMemoryProjectionUnitOfWork(self.read), clock=FixedClock()
        )

    def event(self, body: dict[str, Any]) -> Any:
        return parse_training_event(body)

    def test_authorization_uses_highest_version_and_ignores_older(self) -> None:
        athlete, professional = uuid4(), uuid4()
        newest = self.event(authorization_envelope(athlete, professional, version=3))
        older = self.event(authorization_envelope(athlete, professional, version=2, revoked=True))

        outcomes = [self.project(newest), self.project(newest), self.project(older)]

        self.assertEqual(
            outcomes,
            [ProjectionOutcome.PROJECTED, ProjectionOutcome.DUPLICATE, ProjectionOutcome.STALE],
        )
        view = self.read.authorizations[(athlete, professional)]
        self.assertEqual((view.status, view.version), ("ACTIVE", 3))

    def test_revocation_replaces_the_active_view(self) -> None:
        athlete, professional = uuid4(), uuid4()
        self.project(self.event(authorization_envelope(athlete, professional)))

        self.project(
            self.event(authorization_envelope(athlete, professional, version=2, revoked=True))
        )

        self.assertFalse(self.read.authorizations[(athlete, professional)].is_active)

    def test_session_is_projected_once(self) -> None:
        body = session_envelope(uuid4())
        event = self.event(body)

        outcomes = [self.project(event), self.project(event)]

        self.assertEqual(outcomes, [ProjectionOutcome.PROJECTED, ProjectionOutcome.DUPLICATE])
        self.assertEqual(len(self.read.sessions), 1)

    def test_replayed_session_with_same_version_is_stale(self) -> None:
        body = session_envelope(uuid4())
        self.project(self.event(body))

        replay = self.event({**body, "event_id": str(uuid4())})

        self.assertEqual(self.project(replay), ProjectionOutcome.STALE)


class TrainingHistoryQueryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.read = InMemoryReadStore()
        self.athlete = Caller(uuid4(), "athlete")
        self.professional = Caller(uuid4(), "service_provider")
        self.query = GetTrainingHistory(
            reader=InMemoryHistoryReader(self.read),
            authorizations=InMemoryAuthorizationReader(self.read),
            permissions=PERMISSIONS,
        )

    def add(self, *entries: SessionHistoryEntry) -> None:
        for item in entries:
            self.read.sessions[item.session_id] = item

    def authorize(self, active: bool = True) -> None:
        self.read.authorizations[(self.athlete.user_id, self.professional.user_id)] = (
            AuthorizationView(
                athlete_id=self.athlete.user_id,
                professional_id=self.professional.user_id,
                status="ACTIVE" if active else "REVOKED",
                version=1 if active else 2,
                granted_at=NOW,
                revoked_at=None if active else NOW,
            )
        )

    def test_athlete_filters_by_period_and_sport_in_date_order(self) -> None:
        """CA1: solo sus registros, por periodo o deporte, ordenados por fecha."""
        day = datetime(2026, 10, 5, 8, 0, tzinfo=UTC)
        mine = [entry(self.athlete.user_id, day + timedelta(days=i)) for i in range(5)]
        cycling = entry(self.athlete.user_id, day + timedelta(days=2, hours=3), "cycling")
        other = entry(uuid4(), day + timedelta(days=1))
        self.add(*mine, cycling, other)

        page = self.query.own(
            self.athlete, HistoryFilters(date_from=date(2026, 10, 6), date_to=date(2026, 10, 8))
        )
        by_sport = self.query.own(self.athlete, HistoryFilters(sport=Sport.CYCLING))

        self.assertEqual(
            [item.ended_at for item in page.items],
            sorted(
                [mine[3].ended_at, cycling.ended_at, mine[2].ended_at, mine[1].ended_at],
                reverse=True,
            ),
        )
        self.assertNotIn(other.session_id, {item.session_id for item in page.items})
        self.assertEqual([item.session_id for item in by_sport.items], [cycling.session_id])

    def test_empty_history_returns_available_filters(self) -> None:
        """CA2."""
        page = self.query.own(self.athlete, HistoryFilters())

        self.assertEqual((page.items, page.next_cursor), ((), None))
        self.assertEqual(
            page.available_sports, ("running", "cycling", "swimming", "strength", "other")
        )

    def test_professional_needs_permission_and_active_authorization(self) -> None:
        """CA3."""
        filters = HistoryFilters()
        with self.assertRaises(AthleteAuthorizationRequired):
            self.query.of_athlete(self.professional, self.athlete.user_id, filters)
        self.authorize(active=False)
        with self.assertRaises(AthleteAuthorizationRequired):
            self.query.of_athlete(self.professional, self.athlete.user_id, filters)
        for caller in (Caller(uuid4(), "organizer"), Caller(uuid4(), "athlete")):
            with self.subTest(actor=caller.actor_type), self.assertRaises(PermissionRequired):
                self.query.of_athlete(caller, self.athlete.user_id, filters)
        with self.assertRaises(PermissionRequired):
            self.query.own(self.professional, filters)

        self.authorize()
        self.add(entry(self.athlete.user_id, NOW))
        page = self.query.of_athlete(self.professional, self.athlete.user_id, filters)
        self.assertEqual(len(page.items), 1)

    def test_professional_access_is_audited(self) -> None:
        with self.assertLogs("sportapp.audit", level="INFO") as logs:
            with self.assertRaises(AthleteAuthorizationRequired):
                self.query.of_athlete(self.professional, self.athlete.user_id, HistoryFilters())
            self.authorize()
            self.query.of_athlete(self.professional, self.athlete.user_id, HistoryFilters())

        results = [record.fields["result"] for record in logs.records]  # type: ignore[attr-defined]
        self.assertEqual(results, ["rejected:athlete_authorization_required", "granted"])

    def test_pagination_never_loses_or_repeats_records(self) -> None:
        """CA4: 45 sesiones en páginas de 20, con sesiones nuevas llegando entre páginas."""
        start = datetime(2026, 9, 1, tzinfo=UTC)
        same_instant = start + timedelta(days=10)
        entries = [entry(self.athlete.user_id, start + timedelta(hours=i)) for i in range(40)]
        # Empates en `ended_at`: el orden se desempata por `session_id`.
        entries += [entry(self.athlete.user_id, same_instant) for _ in range(5)]
        self.add(*entries)

        seen: list[UUID] = []
        cursor: str | None = None
        pages = 0
        while True:
            page = self.query.own(self.athlete, HistoryFilters(limit=20, cursor=cursor))
            seen.extend(item.session_id for item in page.items)
            pages += 1
            if pages == 1:
                # Llega una sesión más reciente mientras se pagina: no altera las siguientes.
                self.add(entry(self.athlete.user_id, datetime(2026, 12, 1, tzinfo=UTC)))
            if page.next_cursor is None:
                break
            cursor = page.next_cursor

        self.assertEqual(pages, 3)
        self.assertEqual(len(seen), len(set(seen)), "registro repetido")
        self.assertEqual(set(seen), {item.session_id for item in entries}, "registro perdido")

    def test_invalid_filters_cursor_and_limit(self) -> None:
        cases = (
            (HistoryFilters(date_from=date(2026, 10, 9), date_to=date(2026, 10, 8)), "to"),
            (HistoryFilters(cursor="no-es-un-cursor"), "cursor"),
            (HistoryFilters(limit=0), "limit"),
            (HistoryFilters(limit=101), "limit"),
        )
        for filters, field in cases:
            with self.subTest(field=field), self.assertRaises(InvalidHistoryQuery) as raised:
                self.query.own(self.athlete, filters)
            self.assertEqual(raised.exception.field, field)

    def test_cursor_round_trip(self) -> None:
        self.add(*[entry(self.athlete.user_id, NOW - timedelta(hours=i)) for i in range(3)])

        page = self.query.own(self.athlete, HistoryFilters(limit=2))

        assert page.next_cursor is not None
        position = decode_cursor(page.next_cursor)
        self.assertEqual(position.session_id, page.items[-1].session_id)
        self.assertEqual(position.ended_at, page.items[-1].ended_at)
        with self.assertRaises(InvalidHistoryQuery):
            decode_cursor(page.next_cursor[:-4] + "!!!!")


if __name__ == "__main__":
    unittest.main()
