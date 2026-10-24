"""HU032 sin infraestructura: reglas de la sesión y casos de uso de inicio y fin (ADR-029)."""

import unittest
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid1, uuid4

from fakes import NOW, FakeCapabilities, FixedClock
from session_fakes import (
    InMemorySessionStore,
    InMemorySessionUnitOfWork,
    StoredPlan,
    start_body,
)

from training_management.api.session_payload import parse_finish, parse_start
from training_management.application.commands.training_sessions import (
    FinishTrainingSession,
    FinishTrainingSessionCommand,
    StartTrainingSession,
    StartTrainingSessionCommand,
)
from training_management.application.errors import (
    ActiveSessionExists,
    CapabilitiesUnavailable,
    CapabilityRequired,
    IdempotencyKeyReused,
    PermissionRequired,
    SessionIdReused,
    TrainingPlanNotFound,
    TrainingSessionAlreadyFinished,
    TrainingSessionNotFound,
)
from training_management.application.ports import Caller
from training_management.domain.plans.model import Sport
from training_management.domain.sessions.model import (
    SessionEndReason,
    TrainingSessionStatus,
    validate_start,
)
from training_management.domain.sessions.validation import InvalidTrainingSession

RECORD = "training.session.record"


def start_issues(body: object, now: datetime = NOW) -> set[tuple[str | None, str]]:
    try:
        validate_start(parse_start(body), now)
    except InvalidTrainingSession as error:
        return {(item.field, item.issue.value) for item in error.violations}
    return set()


class StartRulesTest(unittest.TestCase):
    def test_server_owned_and_unknown_fields_are_not_allowed(self) -> None:
        body = start_body(
            athlete_id=str(uuid4()), status="ACTIVE", version=1, duration_seconds=10, notes="x"
        )

        self.assertEqual(
            start_issues(body),
            {
                ("athlete_id", "field_not_allowed"),
                ("status", "field_not_allowed"),
                ("version", "field_not_allowed"),
                ("duration_seconds", "field_not_allowed"),
                ("notes", "field_not_allowed"),
            },
        )

    def test_required_types_and_catalog(self) -> None:
        self.assertEqual(
            start_issues({}),
            {("session_id", "required"), ("sport", "required"), ("started_at", "required")},
        )
        self.assertEqual(
            start_issues(start_body(sport="yoga", plan_id=12)),
            {("sport", "unknown_value"), ("plan_id", "unknown_value")},
        )
        self.assertEqual(start_issues(None), {(None, "required")})

    def test_session_id_must_be_uuid_v4(self) -> None:
        for value in ("no-uuid", str(uuid1())):
            with self.subTest(value=value):
                self.assertEqual(
                    start_issues(start_body(session_id=value)), {("session_id", "invalid_uuid")}
                )
        self.assertEqual(start_issues(start_body(plan_id="x")), {("plan_id", "invalid_uuid")})

    def test_instants_are_utc_seconds_within_the_window(self) -> None:
        """Tolerancia futura de 5 minutos y antigüedad máxima de 30 días (bordes incluidos)."""
        cases: dict[str, set[tuple[str | None, str]]] = {
            "2026-10-12T12:05:00Z": set(),
            "2026-10-12T12:05:01Z": {("started_at", "too_far_in_future")},
            "2026-09-12T12:00:00Z": set(),
            "2026-09-12T11:59:59Z": {("started_at", "too_old")},
            "2026-10-12T11:30:00+00:00": {("started_at", "invalid_datetime")},
            "2026-10-12T06:30:00-05:00": {("started_at", "invalid_datetime")},
            "2026-10-12T11:30:00.500Z": {("started_at", "invalid_datetime")},
            "2026-02-30T11:30:00Z": {("started_at", "invalid_datetime")},
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(start_issues(start_body(started_at=value)), expected)


class StartTrainingSessionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = InMemorySessionStore()
        self.clock = FixedClock()
        self.athlete = Caller(uuid4(), "athlete")
        self.capabilities = FakeCapabilities({self.athlete.user_id: {RECORD}})
        self.start = self.use_case()

    def use_case(self) -> StartTrainingSession:
        return StartTrainingSession(
            unit_of_work=lambda: InMemorySessionUnitOfWork(self.store),
            capabilities=self.capabilities,
            clock=self.clock,
        )

    def command(self, key: UUID | None = None, **overrides: Any) -> StartTrainingSessionCommand:
        return StartTrainingSessionCommand(
            start=parse_start(start_body(**overrides)),
            idempotency_key=key or uuid4(),
            correlation_id=uuid4(),
        )

    def test_starts_an_active_session_with_its_event(self) -> None:
        """CA1."""
        command = self.command()

        result = self.start(self.athlete, command)

        session = result.session
        self.assertEqual((session.status, session.version), (TrainingSessionStatus.ACTIVE, 1))
        self.assertEqual(session.athlete_id, self.athlete.user_id)
        self.assertEqual(session.started_at, datetime(2026, 10, 12, 11, 30, tzinfo=UTC))
        [message] = self.store.outbox
        self.assertEqual(message.event_type, "entrenamiento.iniciado.v1")
        self.assertEqual((message.aggregate_id, message.aggregate_version), (session.session_id, 1))
        self.assertEqual(message.causation_id, str(command.idempotency_key))
        self.assertEqual(
            message.payload,
            {
                "session_id": str(session.session_id),
                "athlete_id": str(self.athlete.user_id),
                "plan_id": None,
                "sport": "running",
                "started_at": "2026-10-12T11:30:00Z",
                "status": "ACTIVE",
            },
        )

    def test_only_athletes_with_the_capability(self) -> None:
        for caller in (Caller(uuid4(), "service_provider"), Caller(uuid4(), "organizer")):
            with self.subTest(actor=caller.actor_type), self.assertRaises(PermissionRequired):
                self.start(caller, self.command())
        with self.assertRaises(CapabilityRequired):
            self.start(Caller(uuid4(), "athlete"), self.command())
        self.capabilities.unavailable = True
        with self.assertRaises(CapabilitiesUnavailable):
            self.start(self.athlete, self.command())
        self.assertEqual((self.store.sessions, self.store.outbox), ({}, []))

    def test_plan_must_be_own_assigned_and_contain_the_sport(self) -> None:
        """CA9: plan ajeno o sin el deporte → no encontrado; la sesión libre no lo requiere."""
        own, foreign = uuid4(), uuid4()
        self.store.plans[own] = StoredPlan(self.athlete.user_id, frozenset({Sport.RUNNING}))
        self.store.plans[foreign] = StoredPlan(uuid4(), frozenset({Sport.RUNNING}))
        for plan_id, sport in ((foreign, "running"), (own, "cycling"), (uuid4(), "running")):
            with self.subTest(sport=sport), self.assertRaises(TrainingPlanNotFound):
                self.start(self.athlete, self.command(plan_id=str(plan_id), sport=sport))

        result = self.start(self.athlete, self.command(plan_id=str(own)))

        self.assertEqual(result.session.plan_id, own)
        self.assertEqual(len(self.store.outbox), 1)

    def test_one_active_session_per_athlete(self) -> None:
        """CA5."""
        self.start(self.athlete, self.command())

        with self.assertRaises(ActiveSessionExists):
            self.start(self.athlete, self.command())
        self.assertEqual(len(self.store.sessions), 1)

    def test_idempotent_retry_and_key_reuse(self) -> None:
        """CA4."""
        key, session_id = uuid4(), str(uuid4())
        first = self.start(self.athlete, self.command(key, session_id=session_id))
        retry = self.start(self.athlete, self.command(key, session_id=session_id))

        self.assertTrue(retry.replayed)
        self.assertEqual(retry.session, first.session)
        self.assertEqual(len(self.store.outbox), 1)
        with self.assertRaises(IdempotencyKeyReused):
            self.start(self.athlete, self.command(key, session_id=session_id, sport="cycling"))

    def test_same_session_id_with_another_key(self) -> None:
        """Mismo inicio con otra clave → la sesión vigente sin otro evento; otros datos → 409."""
        session_id = str(uuid4())
        self.start(self.athlete, self.command(session_id=session_id))

        again = self.start(self.athlete, self.command(session_id=session_id))

        self.assertTrue(again.replayed)
        self.assertEqual(len(self.store.outbox), 1)
        with self.assertRaises(SessionIdReused):
            self.start(
                self.athlete,
                self.command(session_id=session_id, started_at="2026-10-12T11:31:00Z"),
            )
        other = Caller(uuid4(), "athlete")
        self.capabilities.granted[other.user_id] = {RECORD}
        with self.assertRaises(SessionIdReused):
            self.start(other, self.command(session_id=session_id))

    def test_keys_of_other_athletes_and_operations_are_independent(self) -> None:
        key = uuid4()
        other = Caller(uuid4(), "athlete")
        self.capabilities.granted[other.user_id] = {RECORD}

        mine = self.start(self.athlete, self.command(key))
        theirs = self.start(other, self.command(key))

        self.assertFalse(theirs.replayed)
        self.assertNotEqual(mine.session.session_id, theirs.session.session_id)

    def test_concurrent_retry_returns_the_winner(self) -> None:
        """La carrera con la misma clave y sesión devuelve la confirmada primero."""
        key, session_id = uuid4(), str(uuid4())
        winner = self.use_case()
        units = iter(
            [
                InMemorySessionUnitOfWork(self.store),
                InMemorySessionUnitOfWork(self.store),
                InMemorySessionUnitOfWork(
                    self.store,
                    lambda: winner(self.athlete, self.command(key, session_id=session_id)),
                ),
                InMemorySessionUnitOfWork(self.store),
                InMemorySessionUnitOfWork(self.store),
            ]
        )
        loser = StartTrainingSession(
            unit_of_work=units.__next__, capabilities=self.capabilities, clock=self.clock
        )

        result = loser(self.athlete, self.command(key, session_id=session_id))

        self.assertTrue(result.replayed)
        self.assertEqual((len(self.store.sessions), len(self.store.outbox)), (1, 1))

    def test_replay_does_not_check_the_capability_again(self) -> None:
        key, session_id = uuid4(), str(uuid4())
        self.start(self.athlete, self.command(key, session_id=session_id))
        self.capabilities.granted.clear()

        retry = self.start(self.athlete, self.command(key, session_id=session_id))

        self.assertTrue(retry.replayed)
        self.assertEqual(len(self.capabilities.calls), 1)


class FinishTrainingSessionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = InMemorySessionStore()
        self.clock = FixedClock()
        self.athlete = Caller(uuid4(), "athlete")
        self.capabilities = FakeCapabilities({self.athlete.user_id: {RECORD}})
        start = StartTrainingSession(
            unit_of_work=lambda: InMemorySessionUnitOfWork(self.store),
            capabilities=self.capabilities,
            clock=self.clock,
        )
        self.session = start(
            self.athlete,
            StartTrainingSessionCommand(start=parse_start(start_body()), idempotency_key=uuid4()),
        ).session
        self.capabilities.calls.clear()
        self.finish = FinishTrainingSession(
            unit_of_work=lambda: InMemorySessionUnitOfWork(self.store), clock=self.clock
        )

    def command(
        self, ended_at: str = "2026-10-12T11:45:30Z", key: UUID | None = None, **body: Any
    ) -> FinishTrainingSessionCommand:
        session_id = str(body.pop("session_id", self.session.session_id))
        raw, reason, transport = parse_finish({"ended_at": ended_at, **body})
        return FinishTrainingSessionCommand(
            session_id=session_id,
            ended_at=raw,
            idempotency_key=key or uuid4(),
            transport_violations=transport,
            reason=reason,
        )

    def test_finishes_with_backend_duration_and_event(self) -> None:
        """CA2: `FINISHED`, versión 2, duración calculada y `entrenamiento.finalizado.v1`."""
        result = self.finish(self.athlete, self.command())

        finished = result.session
        self.assertEqual((finished.status, finished.version), (TrainingSessionStatus.FINISHED, 2))
        self.assertEqual(finished.duration_seconds, 15 * 60 + 30)
        message = self.store.outbox[-1]
        self.assertEqual(message.event_type, "entrenamiento.finalizado.v1")
        self.assertEqual(message.aggregate_version, 2)
        self.assertEqual(message.payload["duration_seconds"], 930)
        self.assertEqual(message.payload["ended_at"], "2026-10-12T11:45:30Z")
        self.assertEqual(self.capabilities.calls, [], "finalizar no consulta Afiliación")

    def test_equal_instants_give_zero_and_earlier_end_is_rejected(self) -> None:
        with self.assertRaises(InvalidTrainingSession) as raised:
            self.finish(self.athlete, self.command("2026-10-12T11:29:59Z"))
        self.assertEqual(raised.exception.violations[0].issue.value, "ends_before_starts")

        result = self.finish(self.athlete, self.command("2026-10-12T11:30:00Z"))

        self.assertEqual(result.session.duration_seconds, 0)

    def test_missing_foreign_or_finished_sessions(self) -> None:
        """CA3: sin duración inventada ni evento."""
        events = len(self.store.outbox)
        with self.assertRaises(TrainingSessionNotFound):
            self.finish(Caller(uuid4(), "athlete"), self.command())
        with self.assertRaises(TrainingSessionNotFound):
            self.finish(self.athlete, self.command(session_id=uuid4()))
        self.finish(self.athlete, self.command())

        again = self.finish(self.athlete, self.command())
        with self.assertRaises(TrainingSessionAlreadyFinished):
            self.finish(self.athlete, self.command("2026-10-12T11:50:00Z"))

        self.assertTrue(again.replayed)
        self.assertEqual(len(self.store.outbox), events + 1)

    def test_idempotent_finish_and_key_reuse(self) -> None:
        """CA4."""
        key = uuid4()
        first = self.finish(self.athlete, self.command(key=key))
        retry = self.finish(self.athlete, self.command(key=key))

        self.assertTrue(retry.replayed)
        self.assertEqual(retry.session, first.session)
        with self.assertRaises(IdempotencyKeyReused):
            self.finish(self.athlete, self.command("2026-10-12T11:50:00Z", key=key))

    def test_risk_stop_finishes_once(self) -> None:
        """HU036 CA4: `RISK_STOP` se guarda, se repite sin otro evento y no admite otro motivo."""
        first = self.finish(self.athlete, self.command(reason="RISK_STOP"))
        self.assertIs(first.session.end_reason, SessionEndReason.RISK_STOP)
        self.assertEqual(first.session.status, TrainingSessionStatus.FINISHED)
        events = len(self.store.outbox)
        again = self.finish(self.athlete, self.command(reason="RISK_STOP"))
        self.assertTrue(again.replayed)
        self.assertIs(again.session.end_reason, SessionEndReason.RISK_STOP)
        self.assertEqual(len(self.store.outbox), events)
        with self.assertRaises(TrainingSessionAlreadyFinished):
            self.finish(self.athlete, self.command())
        # El payload aprobado de `entrenamiento.finalizado.v1` no cambia.
        self.assertNotIn("end_reason", self.store.outbox[-1].payload)

    def test_same_key_with_another_reason_is_reused(self) -> None:
        key = uuid4()
        self.finish(self.athlete, self.command(key=key))
        with self.assertRaises(IdempotencyKeyReused):
            self.finish(self.athlete, self.command(key=key, reason="RISK_STOP"))

    def test_unknown_reason_is_rejected(self) -> None:
        with self.assertRaises(InvalidTrainingSession) as caught:
            self.finish(self.athlete, self.command(reason="TIRED"))
        self.assertEqual(
            {(item.field, item.issue.value) for item in caught.exception.violations},
            {("reason", "unknown_value")},
        )

    def test_finishing_does_not_require_the_capability(self) -> None:
        self.capabilities.granted.clear()

        result = self.finish(self.athlete, self.command())

        self.assertEqual(result.session.status, TrainingSessionStatus.FINISHED)

    def test_validation_of_body_and_path(self) -> None:
        cases: list[tuple[FinishTrainingSessionCommand, set[tuple[str | None, str]]]] = [
            (self.command(duration_seconds=5), {("duration_seconds", "field_not_allowed")}),
            (self.command("ayer"), {("ended_at", "invalid_datetime")}),
            (self.command(session_id="x"), {("session_id", "invalid_uuid")}),
            (
                self.command((NOW + timedelta(minutes=6)).isoformat().replace("+00:00", "Z")),
                {("ended_at", "too_far_in_future")},
            ),
        ]
        for command, expected in cases:
            with self.subTest(expected=expected), self.assertRaises(InvalidTrainingSession) as e:
                self.finish(self.athlete, command)
            self.assertEqual({(v.field, v.issue.value) for v in e.exception.violations}, expected)


if __name__ == "__main__":
    unittest.main()
