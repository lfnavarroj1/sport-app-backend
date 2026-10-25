"""HU042: caso de uso del reporte de salud (ADR-037), CA1-CA4 sin infraestructura."""

import unittest
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

from fakes import NOW, FixedClock
from health_fakes import (
    CONSENT,
    InMemoryHealthReportUnitOfWork,
    InMemoryHealthStore,
    health_body,
    training_session,
)
from leak_checks import leaks

from training_management.application.commands.report_health import (
    IDEMPOTENCY_TTL,
    OPERATION,
    ReportHealth,
    ReportHealthCommand,
    ReportOutcome,
)
from training_management.application.errors import (
    IdempotencyKeyReused,
    PermissionRequired,
    ReportIdReused,
    TrainingSessionNotFound,
)
from training_management.application.ports import Caller
from training_management.domain.health.events import AGGREGATE_TYPE, EVENT_TYPE
from training_management.domain.health.validation import InvalidHealthReport


class HealthReportTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.store = InMemoryHealthStore()
        self.clock = FixedClock()
        self.athlete = Caller(user_id=uuid4(), actor_type="athlete")
        self.hooks: list[Any] = []
        self.report_health = ReportHealth(
            unit_of_work=self.unit_of_work, clock=self.clock, consent_version=CONSENT
        )

    def unit_of_work(self) -> InMemoryHealthReportUnitOfWork:
        hook = self.hooks.pop(0) if self.hooks else None
        return InMemoryHealthReportUnitOfWork(self.store, before_commit=hook)

    def report(self, body: Any, key: str | None = None, caller: Caller | None = None) -> Any:
        return self.report_health(
            caller or self.athlete,
            ReportHealthCommand(body=body, idempotency_key=key or str(uuid4())),
        )

    def assert_nothing_persisted(self) -> None:
        self.assertEqual(self.store.reports, {})
        self.assertEqual(self.store.outbox, [])
        self.assertEqual(self.store.commands, {})


class CreateTest(HealthReportTestCase):
    def test_creates_an_open_report_owned_by_the_caller(self) -> None:
        """CA1."""
        body = health_body()
        correlation_id = uuid4()
        key = str(uuid4())

        result = self.report_health(
            self.athlete,
            ReportHealthCommand(body=body, idempotency_key=key, correlation_id=correlation_id),
        )

        report = result.report
        self.assertEqual(result.outcome, ReportOutcome.CREATED)
        self.assertEqual(report.report_id, UUID(body["report_id"]))
        self.assertEqual(report.athlete_id, self.athlete.user_id)
        self.assertEqual((report.status.value, report.version), ("OPEN", 1))
        self.assertEqual(report.reported_at, NOW)
        self.assertEqual(self.store.reports[report.report_id], report)
        [message] = self.store.outbox
        self.assertEqual(message.event_type, EVENT_TYPE)
        self.assertEqual(message.aggregate_type, AGGREGATE_TYPE)
        self.assertEqual(message.aggregate_id, report.report_id)
        self.assertEqual(message.aggregate_version, 1)
        self.assertEqual(message.causation_id, key)
        self.assertEqual(message.correlation_id, correlation_id)
        self.assertEqual(message.payload["athlete_id"], str(self.athlete.user_id))
        [record] = self.store.commands.values()
        self.assertEqual(record.operation, OPERATION)
        self.assertEqual(record.expires_at - record.created_at, IDEMPOTENCY_TTL)
        self.assertEqual(IDEMPOTENCY_TTL, timedelta(days=30))

    def test_own_session_is_accepted(self) -> None:
        session = training_session(self.athlete.user_id)
        self.store.sessions[session.session_id] = session

        result = self.report(health_body(session_id=str(session.session_id)))

        self.assertEqual(result.report.session_id, session.session_id)

    def test_foreign_or_missing_session_is_not_found_and_nothing_persists(self) -> None:
        """CA2: sesión ajena o inexistente → 404 sin revelar cuál."""
        foreign = training_session(uuid4())
        self.store.sessions[foreign.session_id] = foreign

        for session_id in (foreign.session_id, uuid4()):
            with self.subTest(session_id=session_id), self.assertRaises(TrainingSessionNotFound):
                self.report(health_body(session_id=str(session_id)))
        self.assert_nothing_persisted()

    def test_only_athletes_can_report(self) -> None:
        for actor_type in ("service_provider", "organizer"):
            with self.subTest(actor_type=actor_type), self.assertRaises(PermissionRequired):
                self.report(health_body(), caller=Caller(uuid4(), actor_type))
        self.assert_nothing_persisted()


class ValidationTest(HealthReportTestCase):
    def test_invalid_body_persists_nothing(self) -> None:
        """CA2."""
        for body in (
            health_body(body_area="TOE"),
            health_body(expected_recovery_days=400),
            health_body(occurred_at="ayer"),
            health_body(consent={"version": "otra"}),
            {k: v for k, v in health_body().items() if k != "consent"},
            health_body(athlete_id=str(uuid4())),
        ):
            with self.subTest(body=body), self.assertRaises(InvalidHealthReport):
                self.report(body)
        self.assert_nothing_persisted()

    def test_missing_or_invalid_key_reports_body_rules_too(self) -> None:
        for key, issue in ((None, "required"), ("   ", "required"), ("clave", "invalid_uuid")):
            with self.subTest(key=key), self.assertRaises(InvalidHealthReport) as raised:
                self.report_health(
                    self.athlete,
                    ReportHealthCommand(body=health_body(severity="X"), idempotency_key=key),
                )
            found = {(item.field, item.issue.value) for item in raised.exception.violations}
            self.assertEqual(found, {("Idempotency-Key", issue), ("severity", "unknown_value")})
        self.assert_nothing_persisted()


class IdempotencyTest(HealthReportTestCase):
    def test_same_key_returns_the_original_response(self) -> None:
        """CA3."""
        body, key = health_body(), str(uuid4())
        first = self.report(body, key)
        self.clock.advance(timedelta(minutes=10))

        again = self.report(body, key)

        self.assertEqual(again.outcome, ReportOutcome.REPLAYED)
        self.assertEqual(again.report, first.report)
        self.assertEqual(len(self.store.outbox), 1)

    def test_same_key_with_other_payload_is_rejected(self) -> None:
        key = str(uuid4())
        self.report(health_body(), key)

        with self.assertRaises(IdempotencyKeyReused):
            self.report(health_body(), key)
        self.assertEqual(len(self.store.outbox), 1)

    def test_keys_are_isolated_per_athlete(self) -> None:
        key = str(uuid4())
        self.report(health_body(), key)

        other = self.report(health_body(), key, caller=Caller(uuid4(), "athlete"))

        self.assertEqual(other.outcome, ReportOutcome.CREATED)
        self.assertEqual(len(self.store.outbox), 2)

    def test_expired_key_is_reused_after_30_days(self) -> None:
        key = str(uuid4())
        self.report(health_body(), key)
        self.clock.advance(timedelta(days=30))

        result = self.report(health_body(), key)

        self.assertEqual(result.outcome, ReportOutcome.CREATED)
        self.assertEqual(len(self.store.commands), 1)

    def test_same_report_id_with_same_data_returns_the_existing_report(self) -> None:
        """CA3: otro intento (otra clave) del mismo reporte, sin otro evento."""
        body = health_body()
        first = self.report(body)

        again = self.report(body)

        self.assertEqual(again.outcome, ReportOutcome.EXISTING)
        self.assertEqual(again.report, first.report)
        self.assertEqual(len(self.store.outbox), 1)

    def test_same_report_id_with_other_data_or_owner_is_rejected(self) -> None:
        """CA3: `409 report_id_reused`."""
        body = health_body()
        self.report(body)

        with self.assertRaises(ReportIdReused):
            self.report({**body, "severity": "HIGH"})
        with self.assertRaises(ReportIdReused):
            self.report(body, caller=Caller(uuid4(), "athlete"))
        self.assertEqual(len(self.store.outbox), 1)

    def test_concurrent_same_key_returns_what_the_winner_confirmed(self) -> None:
        body, key = health_body(), str(uuid4())
        winner: list[Any] = []

        def concurrent() -> None:
            winner.append(self.report(body, key))

        # La primera unidad de trabajo que confirma es la del comando "perdedor": antes de su
        # commit, otra solicitud idéntica confirma primero.
        self.hooks = [None, None, concurrent]

        result = self.report(body, key)

        self.assertEqual(result.report, winner[0].report)
        self.assertEqual(result.outcome, ReportOutcome.REPLAYED)
        self.assertEqual(len(self.store.outbox), 1)
        self.assertEqual(len(self.store.reports), 1)

    def test_concurrent_same_report_id_with_other_key_returns_existing(self) -> None:
        body = health_body()

        def concurrent() -> None:
            self.report(body)

        self.hooks = [None, None, concurrent]

        result = self.report(body)

        self.assertEqual(result.outcome, ReportOutcome.EXISTING)
        self.assertEqual(len(self.store.outbox), 1)


class AuditTest(HealthReportTestCase):
    def test_audit_has_identifiers_but_no_health_data(self) -> None:
        body = health_body(severity="HIGH", body_area="ANKLE_FOOT", type="INJURY")

        with self.assertLogs("sportapp.audit", level="INFO") as logs:
            result = self.report(body)
            with self.assertRaises(ReportIdReused):
                self.report({**body, "severity": "LOW"})

        created, rejected = (record.__dict__["fields"] for record in logs.records)
        self.assertEqual(
            {record.getMessage() for record in logs.records}, {"audit.health_report_created"}
        )
        self.assertEqual(
            created,
            {
                "result": "created",
                "report_id": str(result.report.report_id),
                "athlete_id": str(self.athlete.user_id),
            },
        )
        self.assertEqual(rejected["reason"], "report_id_reused")
        sensitive = ("INJURY", "ANKLE_FOOT", "HIGH", "LOW", "2026-10-12", CONSENT, "3")
        self.assertEqual(leaks(created, sensitive), [])
        self.assertEqual(leaks(rejected, sensitive), [])


if __name__ == "__main__":
    unittest.main()
