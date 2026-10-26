"""Corrida de `terceros-sync-worker` contra PostgreSQL real y el simulador de Strava (HU031,
ADR-042 §8-§11; requiere `TEST_DATABASE_URL`).

`RunSync` con `SqlAlchemySyncRunClaimer`, `SqlAlchemyUnitOfWork` y `StravaHttpGateway` sobre
`StravaSimulator` (nunca Strava real), con la cuenta sembrada con tokens sellados por
`AesGcmTokenSealer`: importación de hasta 3 páginas de 50 con `after` (CA4), evento
normalizado en la outbox (CA4), duplicados y versiones (CA5), `429`/cuota, `5xx`/timeout,
circuito, `401` con renovación, `403`/`404` (CA6), actividad parcial (CA7), desvinculación
concurrente (CA9) y ausencia de tokens en la outbox, la base y los logs.

Los ayudantes de este módulo (sin `TestCase`) los reutilizan `test_outbox_messaging.py` y
`test_exp002_simulator.py`.
"""

import base64
import json
import logging
import os
import secrets
import unittest
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sqlalchemy import Engine, create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from sportapp_platform.migrations import upgrade
from sportapp_platform.testing.postgres import temporary_database
from third_party_management.application.connections import UnlinkStrava
from third_party_management.application.imports import RunSync, SyncOutcome
from third_party_management.application.ports import (
    ActivityPage,
    Caller,
    ProviderGrant,
    ProviderTokens,
    StravaContext,
    StravaGateway,
)
from third_party_management.domain.connections import ExternalAccount, SealedTokens
from third_party_management.domain.synchronization import SyncRun
from third_party_management.infrastructure.clock import SystemClock
from third_party_management.infrastructure.oauth_state import HmacStateCodec
from third_party_management.infrastructure.persistence.models import (
    ExternalAccountRow,
    ImportedActivityRow,
    OutboxEventRow,
    SyncRunRow,
)
from third_party_management.infrastructure.persistence.outbox import SqlAlchemyOutboxStore
from third_party_management.infrastructure.persistence.sync_claims import (
    SqlAlchemySyncRunClaimer,
)
from third_party_management.infrastructure.persistence.unit_of_work import SqlAlchemyUnitOfWork
from third_party_management.infrastructure.strava.circuit import CircuitBreaker
from third_party_management.infrastructure.strava.client import (
    StravaCredentials,
    StravaHttpGateway,
)
from third_party_management.infrastructure.token_cipher import (
    AesGcmTokenSealer,
    derive_state_key,
)
from third_party_management.settings import SyncWorkerSettings
from third_party_management.testing.strava_simulator import (
    StravaSimulator,
    activity,
    ok,
    rate_limited,
    server_error,
    status,
    timeout,
    unreadable,
)
from third_party_management.workers.outbox_publisher import build_envelope, ordering_key
from third_party_management.workers.sync_worker import build_run_sync

HISTORY = "third_party_management.infrastructure.persistence:migrations"
CLIENT_ID = "sportapp-integration"
CONSENT_VERSION = "strava-consent-test.v1"
WORKER = "terceros-sync-it"
LEASE_SECONDS = 300.0
ACTIVITY_START = datetime(2026, 10, 20, 6, 0, tzinfo=UTC)
CAPTURED_LOGGERS = ("sportapp", "httpx")


# ----------------------------------------------------------------- ayudantes compartidos


def new_token_key() -> str:
    """`STRAVA_TOKEN_KEY` sintética: 32 bytes al azar en base64 URL-safe."""
    return base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")


def iso(instant: datetime) -> str:
    return instant.astimezone(UTC).isoformat().replace("+00:00", "Z")


def synthetic_activities(first_id: int, count: int, **overrides: Any) -> list[dict[str, Any]]:
    """`count` actividades válidas con identificadores y minutos de inicio consecutivos."""
    return [
        activity(
            first_id + index,
            start_date=iso(ACTIVITY_START + timedelta(minutes=index)),
            **overrides,
        )
        for index in range(count)
    ]


@dataclass
class ThirdPartyDatabase:
    url: str
    engine: Engine
    sessions: sessionmaker[Session]

    def unit_of_work(self) -> SqlAlchemyUnitOfWork:
        return SqlAlchemyUnitOfWork(self.sessions)

    def count(self, model: Any) -> int:
        with self.sessions() as session:
            return session.scalar(select(func.count()).select_from(model)) or 0

    def rows(self, model: Any) -> list[Any]:
        with self.sessions() as session:
            return list(session.scalars(select(model)))


@contextmanager
def third_party_database(prefix: str) -> Iterator[ThirdPartyDatabase]:
    """Base `terceros` temporal migrada hasta `head`."""
    with temporary_database(prefix) as url:
        upgrade(HISTORY, url)
        engine = create_engine(url, pool_size=5, max_overflow=5)
        try:
            yield ThirdPartyDatabase(url, engine, sessionmaker(engine, expire_on_commit=False))
        finally:
            engine.dispose()


class ManualMonotonic:
    """Reloj del circuito controlado por la prueba (sin esperas reales)."""

    def __init__(self) -> None:
        self.value = 1000.0

    def __call__(self) -> float:
        return self.value


class NoSleep:
    """Registra las esperas de los reintentos sin dormir."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


class AllowAll:
    """Afiliación concede `integrations.strava.connect` (la verificación real está en el
    componente del BFF)."""

    def has_capability(self, user_id: UUID, capability: str) -> bool:
        return True


def strava_context(
    simulator: StravaSimulator,
    key: bytes,
    client_secret: str,
    *,
    breaker: CircuitBreaker | None = None,
    sleep: Callable[[float], None] | None = None,
) -> StravaContext:
    gateway = StravaHttpGateway(
        simulator.client(),
        StravaCredentials(client_id=CLIENT_ID, client_secret=client_secret, redirect_uri=""),
        breaker=breaker or CircuitBreaker(),
        sleep=sleep or NoSleep(),
        jitter=lambda low, high: 0.0,
    )
    return StravaContext(
        gateway=gateway,
        sealer=AesGcmTokenSealer(key),
        states=HmacStateCodec(derive_state_key(key)),
        consent_version=CONSENT_VERSION,
    )


def seed_account(
    db: ThirdPartyDatabase,
    context: StravaContext,
    user_id: UUID,
    tokens: tuple[str, str],
    expires_at: datetime,
) -> None:
    """Vinculación `ACTIVE` como la deja el callback: tokens sellados con AES-256-GCM."""
    access, refresh = tokens
    with db.unit_of_work() as unit_of_work:
        unit_of_work.save_account(
            ExternalAccount.link(
                current=None,
                user_id=user_id,
                external_athlete_id="12345",
                tokens=context.sealer.seal(user_id, ProviderTokens(access, refresh, expires_at)),
                scopes=("read", "activity:read"),
                consent_version=CONSENT_VERSION,
                now=datetime.now(UTC),
            )
        )
        unit_of_work.commit()


def queue_run(db: ThirdPartyDatabase, user_id: UUID) -> SyncRun:
    run = SyncRun.queue(user_id, datetime.now(UTC), uuid4())
    with db.unit_of_work() as unit_of_work:
        unit_of_work.add_run(run)
        unit_of_work.commit()
    return run


def build_sync(
    db: ThirdPartyDatabase,
    context: StravaContext,
    unit_of_work: Callable[[], SqlAlchemyUnitOfWork] | None = None,
) -> RunSync:
    return RunSync(
        claimer=SqlAlchemySyncRunClaimer(db.sessions),
        unit_of_work=unit_of_work or db.unit_of_work,
        strava=context,
        clock=SystemClock(),
        worker_id=WORKER,
        lease_seconds=LEASE_SECONDS,
    )


class LogCapture(logging.Handler):
    """Registros de los loggers del servicio con sus `fields`, para buscar filtraciones."""

    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def install(self, case: unittest.TestCase, *names: str) -> None:
        for name in names:
            logger = logging.getLogger(name)
            previous = logger.level
            logger.addHandler(self)
            logger.setLevel(logging.DEBUG)
            case.addCleanup(logger.setLevel, previous)
            case.addCleanup(logger.removeHandler, self)

    def fields(self, message: str) -> list[dict[str, Any]]:
        return [
            dict(getattr(record, "fields", None) or {})
            for record in self.records
            if record.getMessage() == message
        ]

    def text(self) -> str:
        return "\n".join(
            f"{record.getMessage()} {json.dumps(getattr(record, 'fields', None), default=str)}"
            for record in self.records
        )


# ------------------------------------------------------------------------- pruebas


class _SyncFlowCase(unittest.TestCase):
    strict_tokens = False

    def setUp(self) -> None:
        self.db = self.enterContext(third_party_database("tpm_sync"))
        self.strava = StravaSimulator(strict_tokens=self.strict_tokens)
        self.client_secret = secrets.token_hex(16)
        self.key = base64.urlsafe_b64decode(new_token_key())
        self.monotonic = ManualMonotonic()
        self.breaker = CircuitBreaker(clock=self.monotonic)
        self.sleep = NoSleep()
        self.context = strava_context(
            self.strava, self.key, self.client_secret, breaker=self.breaker, sleep=self.sleep
        )
        self.user_id = uuid4()
        self.tokens = self.strava.issue_tokens()
        self.link(datetime.now(UTC) + timedelta(hours=6))
        self.run_sync = build_sync(self.db, self.context)
        self.logs = LogCapture()
        self.logs.install(self, *CAPTURED_LOGGERS)

    def link(self, expires_at: datetime) -> None:
        seed_account(self.db, self.context, self.user_id, self.tokens, expires_at)

    def sync(self, run_sync: RunSync | None = None) -> tuple[SyncRun, SyncOutcome]:
        run = queue_run(self.db, self.user_id)
        outcome = (run_sync or self.run_sync)()
        assert outcome is not None
        self.assertEqual(outcome.sync_run_id, run.sync_run_id)
        return run, outcome

    def run_row(self, sync_run_id: UUID) -> SyncRunRow:
        with self.db.sessions() as session:
            row = session.get(SyncRunRow, sync_run_id)
        assert row is not None
        return row

    def account_row(self) -> ExternalAccountRow:
        with self.db.sessions() as session:
            row = session.get(ExternalAccountRow, self.user_id)
        assert row is not None
        return row

    def assert_run(
        self, outcome: SyncOutcome, status_value: str, error: str | None, imported: int
    ) -> SyncRunRow:
        row = self.run_row(outcome.sync_run_id)
        self.assertEqual(
            (row.status, row.error_code, row.imported_count), (status_value, error, imported)
        )
        self.assertEqual((outcome.status, outcome.imported_count), (status_value, imported))
        self.assertIsNotNone(row.finished_at)
        self.assertIsNone(row.lease_until, "una corrida terminada no conserva arrendamiento")
        self.assertEqual(row.worker_id, WORKER)
        return row

    def outbox_by_external_id(self) -> dict[str, list[OutboxEventRow]]:
        grouped: dict[str, list[OutboxEventRow]] = {}
        for row in self.db.rows(OutboxEventRow):
            grouped.setdefault(row.payload["external_activity_id"], []).append(row)
        for rows in grouped.values():
            rows.sort(key=lambda row: row.aggregate_version)
        return grouped

    def activity_params(self) -> list[dict[str, list[str]]]:
        return [parse_qs(urlsplit(str(call.url)).query) for call in self.strava.calls("activities")]

    def sensitive(self) -> list[str]:
        return [*self.strava.sensitive_values(), self.client_secret]

    def assert_no_secrets_outside(self) -> None:
        """Ningún token, `client_secret` ni la clave en la outbox, las corridas ni los logs."""
        outbox = json.dumps([row.payload for row in self.db.rows(OutboxEventRow)])
        runs = json.dumps(
            [(row.status, row.error_code, row.worker_id) for row in self.db.rows(SyncRunRow)]
        )
        logs = self.logs.text()
        for value in self.sensitive():
            self.assertNotIn(value, outbox)
            self.assertNotIn(value, runs)
            self.assertNotIn(value, logs)
        for row in self.db.rows(ExternalAccountRow):
            for column in (row.access_token_enc, row.refresh_token_enc):
                for value in self.sensitive():
                    self.assertNotIn(value.encode("utf-8"), bytes(column or b""))


class ImportFlowTest(_SyncFlowCase):
    def test_worker_wiring_imports_at_most_three_pages_of_fifty(self) -> None:
        """CA4: `build_run_sync` con la configuración del worker; 160 actividades → 3 páginas
        de 50 sin `after` en la primera corrida; las 150 recibidas en la outbox."""
        values: dict[str, Any] = {
            "app_env": "test",
            "database_url": self.db.url,
            "strava_client_id": CLIENT_ID,
            "strava_client_secret": self.client_secret,
            "strava_token_key": base64.urlsafe_b64encode(self.key).decode("ascii"),
            "worker_id": WORKER,
        }
        settings = SyncWorkerSettings(**values)
        self.strava.activities = synthetic_activities(5001, 160)

        _, outcome = self.sync(build_run_sync(settings, self.db.sessions, self.strava.client()))

        self.assert_run(outcome, "SUCCEEDED", None, 150)
        self.assertEqual(
            [(params["page"], params["per_page"]) for params in self.activity_params()],
            [(["1"], ["50"]), (["2"], ["50"]), (["3"], ["50"])],
        )
        self.assertTrue(all("after" not in params for params in self.activity_params()))
        self.assertEqual(self.db.count(OutboxEventRow), 150)
        self.assertEqual(self.db.count(ImportedActivityRow), 150)
        self.assertEqual(
            {row.external_activity_id for row in self.db.rows(ImportedActivityRow)},
            {str(5001 + index) for index in range(150)},
        )
        self.assert_no_secrets_outside()

    def test_events_carry_the_normalized_payload_in_the_same_commit(self) -> None:
        """CA4: `actividad.importada.v1` con el sobre, `ordering_key = user_id` y el payload
        `strava-activity.v1` exacto (unidades, redondeo `ROUND_HALF_UP` y zona IANA)."""
        self.strava.activities = [
            activity(1001),
            activity(1002, sport_type="Ride", distance=25000.5, start_date="2026-10-21T12:30:00Z"),
        ]

        run, outcome = self.sync()

        self.assert_run(outcome, "SUCCEEDED", None, 2)
        events = self.outbox_by_external_id()
        [running] = events["1001"]
        activity_id = uuid5(NAMESPACE_URL, "strava:1001")
        self.assertEqual(
            (running.event_type, running.schema_version, running.aggregate_type),
            ("actividad.importada.v1", 1, "external_activity"),
        )
        self.assertEqual((running.aggregate_id, running.aggregate_version), (activity_id, 1))
        self.assertEqual(
            (running.correlation_id, running.causation_id),
            (run.correlation_id, str(run.sync_run_id)),
        )
        self.assertEqual(
            running.payload,
            {
                "activity_id": str(activity_id),
                "user_id": str(self.user_id),
                "provider": "STRAVA",
                "external_activity_id": "1001",
                "sport": "running",
                "name": "Actividad sintética 1001",
                "started_at": "2026-10-20T11:00:00Z",
                "ended_at": "2026-10-20T12:00:00Z",
                "time_zone": "America/Bogota",
                "duration_seconds": 3600,
                "moving_seconds": 3480,
                "distance_meters": 10000,
                "elevation_gain_meters": 86,
                "mapping_version": "strava-activity.v1",
                "version": 1,
            },
        )
        [riding] = events["1002"]
        self.assertEqual(
            (riding.payload["sport"], riding.payload["distance_meters"]), ("cycling", 25001)
        )
        imported = {row.external_activity_id: row for row in self.db.rows(ImportedActivityRow)}
        self.assertEqual(imported["1001"].activity_id, activity_id)
        self.assertEqual(imported["1001"].user_id, self.user_id)
        self.assertEqual(imported["1001"].version, 1)

        claimed = SqlAlchemyOutboxStore(self.db.sessions).claim_batch("relay-it", 10, 30)
        self.assertEqual(len(claimed), 2)
        for event in claimed:
            self.assertEqual(ordering_key(event), str(self.user_id))
            envelope = build_envelope(event)
            self.assertEqual(envelope["producer"], "third-party-management")
            self.assertEqual(envelope["aggregate_id"], envelope["payload"]["activity_id"])
            self.assertTrue(envelope["occurred_at"].endswith("Z"))
        self.assert_no_secrets_outside()

    def test_same_activity_publishes_nothing_and_changes_publish_the_next_version(self) -> None:
        """CA5: misma actividad → nada nuevo; `after` = última importada - 1 día; cambio de
        `elapsed_time` o de `distance` → versión siguiente con el mismo `activity_id`."""
        self.strava.activities = [
            activity(1001, start_date="2026-10-20T11:00:00Z"),
            activity(1002, start_date="2026-10-21T12:30:00Z"),
            activity(1003, start_date="2026-10-22T07:15:00Z"),
        ]
        _, first = self.sync()
        self.assert_run(first, "SUCCEEDED", None, 3)

        _, repeated = self.sync()

        self.assert_run(repeated, "SUCCEEDED", None, 0)
        self.assertEqual(self.db.count(OutboxEventRow), 3)
        expected_after = int(datetime(2026, 10, 21, 7, 15, tzinfo=UTC).timestamp())
        self.assertEqual(self.activity_params()[-1]["after"], [str(expected_after)])

        self.strava.activities = [
            activity(1001, start_date="2026-10-20T11:00:00Z"),
            activity(1002, start_date="2026-10-21T12:30:00Z", distance=12000.7),
            activity(1003, start_date="2026-10-22T07:15:00Z", elapsed_time=4000),
        ]
        _, changed = self.sync()

        self.assert_run(changed, "SUCCEEDED", None, 2)
        events = self.outbox_by_external_id()
        self.assertEqual(len(events["1001"]), 1)
        self.assertEqual([row.aggregate_version for row in events["1002"]], [1, 2])
        self.assertEqual(events["1002"][1].payload["distance_meters"], 12001)
        self.assertEqual(events["1002"][1].payload["version"], 2)
        self.assertEqual(
            events["1002"][0].aggregate_id, events["1002"][1].aggregate_id, "mismo agregado"
        )
        self.assertEqual(events["1003"][1].payload["duration_seconds"], 4000)
        self.assertEqual(events["1003"][1].payload["ended_at"], "2026-10-22T08:21:40Z")
        imported = {row.external_activity_id: row for row in self.db.rows(ImportedActivityRow)}
        self.assertEqual(
            (imported["1001"].version, imported["1002"].version, imported["1003"].version),
            (1, 2, 2),
        )

        _, again = self.sync()
        self.assert_run(again, "SUCCEEDED", None, 0)
        self.assertEqual(self.db.count(OutboxEventRow), 5)

    def test_partial_activity_is_skipped_and_the_valid_ones_are_published(self) -> None:
        """CA7: la incompleta se omite con evidencia (sin datos del proveedor), las válidas sí
        se publican y la corrida termina `FAILED invalid_activity`."""
        self.strava.activities = [
            activity(1001),
            activity(1002, omit=("elapsed_time",), name="Nombre que no debe registrarse"),
            activity(1003, start_date="2026-10-21T08:00:00Z"),
        ]

        _, outcome = self.sync()

        self.assert_run(outcome, "FAILED", "invalid_activity", 2)
        self.assertEqual(outcome.skipped_count, 1)
        self.assertEqual(set(self.outbox_by_external_id()), {"1001", "1003"})
        self.assertEqual(
            {row.external_activity_id for row in self.db.rows(ImportedActivityRow)},
            {"1001", "1003"},
        )
        [skipped] = self.logs.fields("sync.activity_skipped")
        self.assertEqual(
            (skipped["external_activity_id"], skipped["reason"]), ("1002", "invalid_elapsed_time")
        )
        self.assertNotIn("Nombre que no debe registrarse", self.logs.text())


class ProviderFailureFlowTest(_SyncFlowCase):
    def test_rate_limit_stops_without_retrying(self) -> None:
        """CA6: `429` con `Retry-After` → `RATE_LIMITED rate_limited`, una sola llamada."""
        self.strava.activities = synthetic_activities(1001, 3)
        self.strava.queue("activities", rate_limited(retry_after=900))

        _, outcome = self.sync()

        self.assert_run(outcome, "RATE_LIMITED", "rate_limited", 0)
        self.assertEqual(len(self.strava.calls("activities")), 1)
        self.assertEqual(self.sleep.delays, [])
        self.assertEqual(self.db.count(OutboxEventRow), 0)
        [warning] = self.logs.fields("sync.rate_limited")
        self.assertEqual(warning["retry_after_seconds"], 900)

    def test_rate_limit_on_the_second_page_keeps_the_first(self) -> None:
        """CA6: las páginas ya recibidas sí se importan."""
        self.strava.activities = synthetic_activities(1001, 60)
        self.strava.queue("activities", ok(), rate_limited())

        _, outcome = self.sync()

        self.assert_run(outcome, "RATE_LIMITED", "rate_limited", 50)
        self.assertEqual(self.db.count(OutboxEventRow), 50)

    def test_exhausted_quota_stops_after_the_page(self) -> None:
        """CA6: `X-RateLimit-Usage` alcanza el límite tras la primera página."""
        self.strava.activities = synthetic_activities(1001, 60)
        self.strava.rate_limit = (1, 1000)

        _, outcome = self.sync()

        self.assert_run(outcome, "RATE_LIMITED", "rate_limited", 50)
        self.assertEqual(len(self.strava.calls("activities")), 1)

    def test_server_errors_and_timeouts_retry_twice_then_fail(self) -> None:
        """CA6: `5xx`/timeout → 2 reintentos con espera y luego `FAILED provider_unavailable`."""
        self.strava.activities = synthetic_activities(1001, 2)
        self.strava.queue("activities", server_error(), timeout("read"), timeout("connect"))

        _, outcome = self.sync()

        self.assert_run(outcome, "FAILED", "provider_unavailable", 0)
        self.assertEqual(len(self.strava.calls("activities")), 3)
        self.assertEqual(len(self.sleep.delays), 2)
        self.assertEqual(self.db.count(OutboxEventRow), 0)
        self.assertEqual(self.account_row().status, "ACTIVE", "una caída no expira la cuenta")

    def test_transient_failures_recover_within_the_run(self) -> None:
        self.strava.activities = synthetic_activities(1001, 2)
        self.strava.queue("activities", server_error(502), timeout("read"))

        _, outcome = self.sync()

        self.assert_run(outcome, "SUCCEEDED", None, 2)
        self.assertEqual(len(self.strava.calls("activities")), 3)

    def test_circuit_opens_after_five_failures_for_sixty_seconds(self) -> None:
        """CA6: 5 fallos consecutivos abren el circuito 60 s: no se llama a Strava
        (`circuit_open`); vencido el plazo, la siguiente llamada pasa y lo cierra."""
        self.strava.activities = synthetic_activities(1001, 2)
        self.strava.queue("activities", *(server_error() for _ in range(5)))

        _, first = self.sync()
        _, second = self.sync()

        self.assert_run(first, "FAILED", "provider_unavailable", 0)
        self.assert_run(second, "FAILED", "circuit_open", 0)
        self.assertEqual(len(self.strava.calls("activities")), 5)
        self.assertTrue(self.breaker.is_open)

        _, blocked = self.sync()

        self.assert_run(blocked, "FAILED", "circuit_open", 0)
        self.assertEqual(len(self.strava.calls("activities")), 5, "abierto no llama a Strava")

        self.monotonic.value += 61
        _, recovered = self.sync()

        self.assert_run(recovered, "SUCCEEDED", None, 2)
        self.assertFalse(self.breaker.is_open)

    def test_forbidden_not_found_and_unreadable_map_to_the_catalog(self) -> None:
        """CA6: `403` → `insufficient_scope`; `404` → `provider_rejected`; cuerpo ilegible →
        `invalid_response`. La cuenta sigue `ACTIVE`."""
        self.strava.activities = synthetic_activities(1001, 2)
        cases = (
            (status(403), "insufficient_scope"),
            (status(404), "provider_rejected"),
            (unreadable(), "invalid_response"),
        )
        for outcome_programmed, error in cases:
            with self.subTest(error=error):
                self.strava.queue("activities", outcome_programmed)

                _, outcome = self.sync()

                self.assert_run(outcome, "FAILED", error, 0)
                self.assertEqual(self.account_row().status, "ACTIVE")
        self.assertEqual(self.db.count(OutboxEventRow), 0)


class TokenRenewalFlowTest(_SyncFlowCase):
    strict_tokens = True

    def stored_tokens(self) -> ProviderTokens:
        row = self.account_row()
        assert row.access_token_enc is not None
        assert row.refresh_token_enc is not None
        assert row.token_expires_at is not None
        return self.context.sealer.open(
            self.user_id,
            SealedTokens(
                access=bytes(row.access_token_enc),
                refresh=bytes(row.refresh_token_enc),
                expires_at=row.token_expires_at,
            ),
        )

    def test_unauthorized_renews_once_repeats_the_page_and_stores_sealed_tokens(self) -> None:
        """CA6: `401` → renovación y repetición de la página; los tokens nuevos quedan
        cifrados (la vinculación no cambia de versión) y no aparecen en claro."""
        self.strava.activities = synthetic_activities(1001, 2)
        self.strava.expire_access_tokens()

        _, outcome = self.sync()

        self.assert_run(outcome, "SUCCEEDED", None, 2)
        self.assertEqual(len(self.strava.calls("refresh")), 1)
        self.assertEqual(len(self.strava.calls("activities")), 2)
        renewed_access, renewed_refresh = self.strava.issued_tokens[-1]
        self.assertNotEqual(renewed_access, self.tokens[0])
        stored = self.stored_tokens()
        self.assertEqual(
            (stored.access_token, stored.refresh_token), (renewed_access, renewed_refresh)
        )
        row = self.account_row()
        self.assertEqual((row.status, row.version), ("ACTIVE", 1))
        assert row.token_expires_at is not None
        self.assertGreater(row.token_expires_at, datetime.now(UTC) + timedelta(hours=5))
        self.assert_no_secrets_outside()

    def test_renews_before_listing_when_less_than_five_minutes_remain(self) -> None:
        self.link(datetime.now(UTC) + timedelta(minutes=2))
        self.strava.activities = synthetic_activities(1001, 1)

        _, outcome = self.sync()

        self.assert_run(outcome, "SUCCEEDED", None, 1)
        [listing] = self.strava.calls("activities")
        renewed_access, _ = self.strava.issued_tokens[-1]
        self.assertEqual(listing.headers["Authorization"], f"Bearer {renewed_access}")
        self.assertEqual(self.stored_tokens().access_token, renewed_access)
        self.assert_no_secrets_outside()

    def test_rejected_renewal_expires_the_account(self) -> None:
        """CA6: renovación rechazada → cuenta `EXPIRED` sin tokens y `token_expired`; la
        siguiente corrida falla sin llamar a Strava."""
        self.strava.activities = synthetic_activities(1001, 2)
        self.strava.expire_access_tokens()
        self.strava.queue("refresh", status(400))

        _, outcome = self.sync()

        self.assert_run(outcome, "FAILED", "token_expired", 0)
        row = self.account_row()
        self.assertEqual((row.status, row.version), ("EXPIRED", 2))
        self.assertEqual(
            (row.access_token_enc, row.refresh_token_enc, row.token_expires_at),
            (None, None, None),
        )
        calls = len(self.strava.requests)

        _, later = self.sync()

        self.assert_run(later, "FAILED", "token_expired", 0)
        self.assertEqual(len(self.strava.requests), calls)
        self.assert_no_secrets_outside()

    def test_unauthorized_after_renewal_expires_the_account(self) -> None:
        self.strava.activities = synthetic_activities(1001, 2)
        self.strava.queue("activities", status(401), status(401))

        _, outcome = self.sync()

        self.assert_run(outcome, "FAILED", "token_expired", 0)
        self.assertEqual(len(self.strava.calls("refresh")), 1)
        self.assertEqual(self.account_row().status, "EXPIRED")


class _UnlinkDuringFetch:
    """Gateway que desvincula al deportista justo después de recibir la primera página,
    mientras la corrida está `RUNNING` y sin transacción abierta."""

    def __init__(self, gateway: StravaGateway, unlink: Callable[[], None]) -> None:
        self._gateway = gateway
        self._unlink = unlink
        self.unlinked = False

    def authorization_url(self, state: str) -> str:
        return self._gateway.authorization_url(state)

    def exchange_code(self, code: str) -> ProviderGrant:
        return self._gateway.exchange_code(code)

    def refresh(self, refresh_token: str) -> ProviderTokens:
        return self._gateway.refresh(refresh_token)

    def deauthorize(self, access_token: str) -> None:
        self._gateway.deauthorize(access_token)

    def list_activities(
        self, access_token: str, *, after: datetime | None, page: int, per_page: int
    ) -> ActivityPage:
        result = self._gateway.list_activities(
            access_token, after=after, page=page, per_page=per_page
        )
        if not self.unlinked:
            self.unlinked = True
            self._unlink()
        return result


class ConcurrentUnlinkFlowTest(_SyncFlowCase):
    def test_unlinking_while_running_cancels_the_run_and_writes_nothing(self) -> None:
        """CA9: la desvinculación concurrente deja la corrida `FAILED integration_revoked`;
        el cierre no sobrescribe nada: ni actividades, ni outbox, ni tokens."""
        self.strava.activities = synthetic_activities(1001, 3)
        unlink = UnlinkStrava(
            unit_of_work=self.db.unit_of_work,
            capabilities=AllowAll(),
            strava=self.context,
            clock=SystemClock(),
        )
        gateway = _UnlinkDuringFetch(
            self.context.gateway, lambda: unlink(Caller(self.user_id, "athlete"))
        )
        run_sync = build_sync(self.db, replace(self.context, gateway=gateway))

        run, outcome = self.sync(run_sync)

        self.assertTrue(gateway.unlinked)
        self.assertTrue(outcome.cancelled)
        self.assertEqual((outcome.status, outcome.imported_count), ("CANCELLED", 0))
        row = self.run_row(run.sync_run_id)
        self.assertEqual(
            (row.status, row.error_code, row.imported_count),
            ("FAILED", "integration_revoked", 0),
        )
        self.assertEqual(self.db.count(OutboxEventRow), 0)
        self.assertEqual(self.db.count(ImportedActivityRow), 0)
        account = self.account_row()
        self.assertEqual((account.status, account.access_token_enc), ("REVOKED", None))
        self.assertEqual(len(self.strava.calls("deauthorize")), 1)
        self.assert_no_secrets_outside()


class NoPendingRunTest(_SyncFlowCase):
    def test_without_pending_runs_nothing_is_claimed(self) -> None:
        self.assertIsNone(self.run_sync())
        self.assertEqual(self.strava.requests, [])


if __name__ == "__main__":
    unittest.main()
