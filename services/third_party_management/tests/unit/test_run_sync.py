"""Corrida de `terceros-sync-worker` con el simulador de Strava y dobles en memoria
(ADR-042 §8-§11): CA4 (importación y evento normalizado), CA5 (duplicados y versiones),
CA6 (`429`, `5xx`, timeout, circuito y renovación), CA7 (parciales) y CA9 (cancelación)."""

import logging
import unittest
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from fakes import (
    NOW,
    FixedClock,
    InMemoryClaimer,
    InMemoryStore,
    InMemoryUnitOfWork,
    ManualMonotonic,
    NoSleep,
    linked_account,
    strava_context,
)
from leak_checks import leaks

from third_party_management.application.imports import RunSync, SyncOutcome
from third_party_management.domain.connections import AccountStatus
from third_party_management.domain.synchronization import SyncErrorCode, SyncRun, SyncStatus
from third_party_management.infrastructure.strava.circuit import CircuitBreaker
from third_party_management.testing.strava_simulator import (
    StravaSimulator,
    activity,
    page,
    rate_limited,
    server_error,
    status,
    timeout,
    unreadable,
)

WORKER = "worker-1"


def iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


class _Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self.store = InMemoryStore()
        self.clock = FixedClock()
        self.strava = StravaSimulator(strict_tokens=True)
        self.monotonic = ManualMonotonic()
        self.breaker = CircuitBreaker(clock=self.monotonic)
        self.context = strava_context(self.strava, breaker=self.breaker, sleep=NoSleep())
        self.user_id = uuid4()
        self.access, self.refresh = self.strava.issue_tokens()
        self.link(expires_at=NOW + timedelta(hours=6))
        self.claimer = InMemoryClaimer(self.store, self.clock)
        self.run_sync = RunSync(
            claimer=self.claimer,
            unit_of_work=lambda: InMemoryUnitOfWork(self.store),
            strava=self.context,
            clock=self.clock,
            worker_id=WORKER,
            lease_seconds=300,
        )

    def link(self, *, expires_at: datetime) -> None:
        self.store.accounts[self.user_id] = linked_account(
            self.context,
            self.user_id,
            access=self.access,
            refresh=self.refresh,
            expires_at=expires_at,
        )

    def queue_run(self) -> SyncRun:
        run = SyncRun.queue(self.user_id, self.clock.now(), uuid4())
        self.store.runs[run.sync_run_id] = run
        return run

    def sync(self) -> SyncOutcome:
        self.queue_run()
        outcome = self.run_sync()
        assert outcome is not None
        return outcome

    def stored_run(self, outcome: SyncOutcome) -> SyncRun:
        return self.store.runs[outcome.sync_run_id]

    def assert_finished(
        self, outcome: SyncOutcome, status_value: SyncStatus, error: SyncErrorCode | None
    ) -> SyncRun:
        run = self.stored_run(outcome)
        self.assertEqual((run.status, run.error_code), (status_value, error))
        self.assertEqual(run.finished_at, self.clock.now())
        self.assertEqual(run.imported_count, outcome.imported_count)
        return run


class ImportTest(_Fixture):
    def test_imports_new_activities_as_normalized_events(self) -> None:
        """CA4: evento `actividad.importada.v1` con el payload exacto y sin tokens."""
        self.strava.activities = [
            activity(1001, distance=10000.5, elapsed_time=3725, moving_time=3600),
            activity(1002, sport_type="Ride", start_date="2026-10-21T12:30:00Z"),
        ]
        run = self.queue_run()

        outcome = self.run_sync()

        assert outcome is not None
        self.assert_finished(outcome, SyncStatus.SUCCEEDED, None)
        self.assertEqual((outcome.imported_count, outcome.skipped_count), (2, 0))
        first, second = self.store.outbox
        activity_id = uuid5(NAMESPACE_URL, "strava:1001")
        self.assertEqual(
            (first.event_type, first.schema_version, first.aggregate_type),
            ("actividad.importada.v1", 1, "external_activity"),
        )
        self.assertEqual((first.aggregate_id, first.aggregate_version), (activity_id, 1))
        self.assertEqual(first.causation_id, str(run.sync_run_id))
        self.assertEqual(first.correlation_id, run.correlation_id)
        self.assertEqual(first.occurred_at, NOW)
        self.assertEqual(
            first.payload,
            {
                "activity_id": str(activity_id),
                "user_id": str(self.user_id),
                "provider": "STRAVA",
                "external_activity_id": "1001",
                "sport": "running",
                "name": "Actividad sintética 1001",
                "started_at": "2026-10-20T11:00:00Z",
                "ended_at": "2026-10-20T12:02:05Z",
                "time_zone": "America/Bogota",
                "duration_seconds": 3725,
                "moving_seconds": 3600,
                "distance_meters": 10001,
                "elevation_gain_meters": 86,
                "mapping_version": "strava-activity.v1",
                "version": 1,
            },
        )
        self.assertEqual(second.payload["sport"], "cycling")
        self.assertEqual(self.store.imported["1001"].version, 1)
        # Primera corrida: sin `after`; una sola página (menos de 50 actividades).
        [request] = self.strava.calls("activities")
        self.assertEqual(dict(request.url.params), {"per_page": "50", "page": "1"})
        sensitive = self.strava.sensitive_values()
        for message in self.store.outbox:
            self.assertEqual(leaks(message.payload, sensitive), [])

    def test_same_hash_publishes_nothing_and_after_is_last_start_minus_a_day(self) -> None:
        """CA5: una entrega repetida no publica."""
        self.strava.activities = [activity(1001, start_date="2026-10-20T11:00:00Z")]
        self.sync()
        self.clock.advance(timedelta(hours=1))

        outcome = self.sync()

        self.assert_finished(outcome, SyncStatus.SUCCEEDED, None)
        self.assertEqual(outcome.imported_count, 0)
        self.assertEqual(len(self.store.outbox), 1)
        after = int(datetime(2026, 10, 19, 11, 0, tzinfo=UTC).timestamp())
        self.assertEqual(self.strava.calls("activities")[-1].url.params["after"], str(after))

    def test_changed_activity_publishes_the_next_version(self) -> None:
        """CA5: hash distinto → versión mayor, mismo `activity_id`."""
        self.strava.activities = [activity(1001)]
        self.sync()
        self.strava.activities = [activity(1001, distance=10500.0)]
        self.clock.advance(timedelta(hours=1))

        self.sync()

        first, second = self.store.outbox
        self.assertEqual(second.aggregate_id, first.aggregate_id)
        self.assertEqual((second.aggregate_version, second.payload["version"]), (2, 2))
        self.assertEqual(second.payload["distance_meters"], 10500)
        self.assertEqual(self.store.imported["1001"].version, 2)

    def test_duplicate_in_the_same_page_is_published_once(self) -> None:
        self.strava.queue("activities", page([activity(5), activity(5)]))

        outcome = self.sync()

        self.assertEqual(outcome.imported_count, 1)
        self.assertEqual(len(self.store.outbox), 1)

    def test_up_to_three_pages_of_fifty(self) -> None:
        self.strava.activities = [activity(index) for index in range(1, 201)]

        outcome = self.sync()

        self.assertEqual(len(self.strava.calls("activities")), 3)
        self.assertEqual(outcome.imported_count, 150)
        self.assertEqual(
            [request.url.params["page"] for request in self.strava.calls("activities")],
            ["1", "2", "3"],
        )

    def test_partial_activity_is_skipped_and_run_fails_with_evidence(self) -> None:
        """CA7: la parcial se omite y se registra sin datos sensibles; las válidas sí."""
        self.strava.activities = [
            activity(1),
            activity(2, omit=("elapsed_time",)),
            {"name": "sin id"},
            activity(3),
        ]

        with self.assertLogs("sportapp.sync", level="INFO") as logs:
            outcome = self.sync()

        self.assert_finished(outcome, SyncStatus.FAILED, SyncErrorCode.INVALID_ACTIVITY)
        self.assertEqual((outcome.imported_count, outcome.skipped_count), (2, 2))
        self.assertEqual(
            [message.payload["external_activity_id"] for message in self.store.outbox],
            ["1", "3"],
        )
        skipped = [
            record.fields  # type: ignore[attr-defined]
            for record in logs.records
            if record.getMessage() == "sync.activity_skipped"
        ]
        self.assertEqual(
            [(item["external_activity_id"], item["reason"]) for item in skipped],
            [("2", "invalid_elapsed_time"), (None, "invalid_id")],
        )
        self.assertNotIn("2", self.store.imported)

    def test_no_run_pending(self) -> None:
        self.assertIsNone(self.run_sync())


class ProviderFailureTest(_Fixture):
    def test_rate_limited_on_first_page(self) -> None:
        """CA6: `429` → `RATE_LIMITED` sin reintento."""
        self.strava.queue("activities", rate_limited(retry_after=900))

        with self.assertLogs("sportapp.sync", level="WARNING"):
            outcome = self.sync()

        self.assert_finished(outcome, SyncStatus.RATE_LIMITED, SyncErrorCode.RATE_LIMITED)
        self.assertEqual(len(self.strava.calls("activities")), 1)
        self.assertEqual(self.store.outbox, [])

    def test_rate_limited_on_second_page_keeps_the_first(self) -> None:
        self.strava.activities = [activity(index) for index in range(1, 61)]
        self.strava.queue("activities", page(self.strava.activities[:50]), rate_limited())

        outcome = self.sync()

        self.assert_finished(outcome, SyncStatus.RATE_LIMITED, SyncErrorCode.RATE_LIMITED)
        self.assertEqual(outcome.imported_count, 50)

    def test_quota_exhausted_after_a_page_stops_the_run(self) -> None:
        """`X-RateLimit-Usage` alcanza `X-RateLimit-Limit`: no se pide la página siguiente."""
        self.strava.activities = [activity(index) for index in range(1, 101)]
        self.strava.rate_limit = (100, 1000)
        self.strava.usage = [99, 0]

        outcome = self.sync()

        self.assert_finished(outcome, SyncStatus.RATE_LIMITED, SyncErrorCode.RATE_LIMITED)
        self.assertEqual(len(self.strava.calls("activities")), 1)
        self.assertEqual(outcome.imported_count, 50)

    def test_server_errors_and_timeouts_after_retries(self) -> None:
        """CA6: 2 reintentos y luego `FAILED` `provider_unavailable`; la cuenta sigue activa."""
        self.strava.queue("activities", server_error(), timeout("read"), server_error(502))

        outcome = self.sync()

        self.assert_finished(outcome, SyncStatus.FAILED, SyncErrorCode.PROVIDER_UNAVAILABLE)
        self.assertEqual(len(self.strava.calls("activities")), 3)
        self.assertIs(self.store.accounts[self.user_id].status, AccountStatus.ACTIVE)

    def test_transient_failure_recovers_within_the_run(self) -> None:
        self.strava.activities = [activity(1)]
        self.strava.queue("activities", timeout("connect"))

        outcome = self.sync()

        self.assert_finished(outcome, SyncStatus.SUCCEEDED, None)
        self.assertEqual(outcome.imported_count, 1)

    def test_circuit_open_does_not_call_strava(self) -> None:
        for _ in range(5):
            self.breaker.record_failure()

        outcome = self.sync()

        self.assert_finished(outcome, SyncStatus.FAILED, SyncErrorCode.CIRCUIT_OPEN)
        self.assertEqual(self.strava.calls("activities"), [])

    def test_client_errors_map_to_the_catalog(self) -> None:
        cases = [
            (status(403), SyncErrorCode.INSUFFICIENT_SCOPE),
            (status(404), SyncErrorCode.PROVIDER_REJECTED),
            (unreadable(), SyncErrorCode.INVALID_RESPONSE),
            (status(200, json={"pagina": "rota"}), SyncErrorCode.INVALID_RESPONSE),
        ]
        for outcome_programmed, error in cases:
            with self.subTest(error=error):
                self.strava.queue("activities", outcome_programmed)
                outcome = self.sync()
                self.assert_finished(outcome, SyncStatus.FAILED, error)
        self.assertIs(self.store.accounts[self.user_id].status, AccountStatus.ACTIVE)

    def test_unreadable_page_stops_but_keeps_previous_pages(self) -> None:
        self.strava.activities = [activity(index) for index in range(1, 61)]
        self.strava.queue("activities", page(self.strava.activities[:50]), unreadable())

        outcome = self.sync()

        self.assert_finished(outcome, SyncStatus.FAILED, SyncErrorCode.INVALID_RESPONSE)
        self.assertEqual(outcome.imported_count, 50)


class TokenRenewalTest(_Fixture):
    def opened(self) -> Any:
        account = self.store.accounts[self.user_id]
        assert account.tokens is not None
        return self.context.sealer.open(self.user_id, account.tokens)

    def test_unauthorized_renews_once_and_repeats_the_page(self) -> None:
        self.strava.activities = [activity(1)]
        self.strava.expire_access_tokens()

        outcome = self.sync()

        self.assert_finished(outcome, SyncStatus.SUCCEEDED, None)
        self.assertEqual(len(self.strava.calls("refresh")), 1)
        self.assertEqual(len(self.strava.calls("activities")), 2)
        new_access, new_refresh = self.strava.issued_tokens[-1]
        tokens = self.opened()
        self.assertEqual((tokens.access_token, tokens.refresh_token), (new_access, new_refresh))
        self.assertEqual(self.store.accounts[self.user_id].version, 1)

    def test_unauthorized_after_renewal_expires_the_account(self) -> None:
        self.strava.queue("activities", status(401), status(401))

        outcome = self.sync()

        self.assert_finished(outcome, SyncStatus.FAILED, SyncErrorCode.TOKEN_EXPIRED)
        account = self.store.accounts[self.user_id]
        self.assertEqual(
            (account.status, account.tokens, account.version), (AccountStatus.EXPIRED, None, 2)
        )

    def test_renews_before_listing_when_less_than_five_minutes_remain(self) -> None:
        self.link(expires_at=NOW + timedelta(minutes=4))
        self.strava.activities = [activity(1)]

        self.sync()

        self.assertEqual(len(self.strava.calls("refresh")), 1)
        self.assertEqual(len(self.strava.calls("activities")), 1)
        self.assertEqual(
            self.strava.calls("activities")[0].headers["Authorization"],
            f"Bearer {self.strava.issued_tokens[-1][0]}",
        )

    def test_rejected_renewal_expires_the_account(self) -> None:
        self.link(expires_at=NOW - timedelta(minutes=1))
        self.strava.queue("refresh", status(400))

        outcome = self.sync()

        self.assert_finished(outcome, SyncStatus.FAILED, SyncErrorCode.TOKEN_EXPIRED)
        self.assertIs(self.store.accounts[self.user_id].status, AccountStatus.EXPIRED)
        self.assertEqual(self.strava.calls("activities"), [])

    def test_unavailable_renewal_keeps_the_account(self) -> None:
        self.link(expires_at=NOW + timedelta(minutes=1))
        self.strava.queue("refresh", server_error(), server_error(), server_error())

        outcome = self.sync()

        self.assert_finished(outcome, SyncStatus.FAILED, SyncErrorCode.PROVIDER_UNAVAILABLE)
        self.assertIs(self.store.accounts[self.user_id].status, AccountStatus.ACTIVE)

    def test_expired_account_fails_without_calling_strava(self) -> None:
        account = self.store.accounts[self.user_id]
        self.store.accounts[self.user_id] = account.expire(NOW)

        outcome = self.sync()

        self.assert_finished(outcome, SyncStatus.FAILED, SyncErrorCode.TOKEN_EXPIRED)
        self.assertEqual(self.strava.requests, [])


class CancellationTest(_Fixture):
    def test_revoked_account_fails_the_run(self) -> None:
        self.store.accounts[self.user_id] = self.store.accounts[self.user_id].revoke(NOW)

        outcome = self.sync()

        self.assert_finished(outcome, SyncStatus.FAILED, SyncErrorCode.INTEGRATION_REVOKED)
        self.assertEqual(self.strava.requests, [])

    def test_run_cancelled_while_running_is_not_overwritten(self) -> None:
        """CA9: una desvinculación concurrente canceló la corrida; nada se escribe."""
        self.strava.activities = [activity(1)]
        claim = self.claimer.claim

        def claim_then_cancel(worker_id: str, lease_seconds: float) -> SyncRun | None:
            run = claim(worker_id, lease_seconds)
            assert run is not None
            self.store.runs[run.sync_run_id] = run.cancel(NOW)
            return run

        self.claimer.claim = claim_then_cancel  # type: ignore[method-assign]

        outcome = self.sync()

        self.assertTrue(outcome.cancelled)
        run = self.stored_run(outcome)
        self.assertEqual(
            (run.status, run.error_code), (SyncStatus.FAILED, SyncErrorCode.INTEGRATION_REVOKED)
        )
        self.assertEqual(self.store.outbox, [])
        self.assertEqual(self.store.imported, {})

    def test_run_reclaimed_by_another_worker_is_not_overwritten(self) -> None:
        self.strava.activities = [activity(1)]
        claim = self.claimer.claim

        def claim_then_steal(worker_id: str, lease_seconds: float) -> SyncRun | None:
            run = claim(worker_id, lease_seconds)
            assert run is not None
            self.store.run_workers[run.sync_run_id] = "worker-2"
            return run

        self.claimer.claim = claim_then_steal  # type: ignore[method-assign]

        outcome = self.sync()

        self.assertTrue(outcome.cancelled)
        self.assertEqual(self.store.outbox, [])


class SyncLogsTest(_Fixture):
    def test_started_and_finished_without_secrets(self) -> None:
        self.strava.activities = [activity(1), activity(2, omit=("distance",))]
        self.strava.expire_access_tokens()
        logger = logging.getLogger()

        with self.assertLogs(logger, level="INFO") as logs:
            outcome = self.sync()

        messages = [record.getMessage() for record in logs.records]
        self.assertIn("sync.started", messages)
        self.assertIn("sync.finished", messages)
        finished = next(
            record.fields  # type: ignore[attr-defined]
            for record in logs.records
            if record.getMessage() == "sync.finished"
        )
        self.assertEqual(finished["sync_run_id"], str(outcome.sync_run_id))
        self.assertEqual(finished["user_id"], str(self.user_id))
        self.assertEqual(
            (finished["status"], finished["error_code"], finished["imported_count"]),
            ("FAILED", "invalid_activity", 1),
        )
        sensitive = self.strava.sensitive_values()
        for record in logs.records:
            fields = getattr(record, "fields", {})
            self.assertEqual(leaks(fields, sensitive), [], record.getMessage())
            for value in sensitive:
                self.assertNotIn(value, record.getMessage())

    def test_outcome_identifies_the_run(self) -> None:
        run = self.queue_run()

        outcome = self.run_sync()

        assert outcome is not None
        self.assertEqual(outcome.sync_run_id, run.sync_run_id)
        self.assertIsInstance(outcome.sync_run_id, UUID)


if __name__ == "__main__":
    unittest.main()
