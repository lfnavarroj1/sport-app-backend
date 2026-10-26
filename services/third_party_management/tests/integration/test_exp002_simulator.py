"""EXP-002 / HA012 con el simulador de Strava (HU031, ADR-042 §14; requiere `TEST_DATABASE_URL`).

`RUNS` corridas de sincronización de `ACTIVITIES_PER_RUN` actividades nuevas cada una contra
`StravaSimulator` (nunca Strava real) y la base `terceros` real, después de una corrida de
calentamiento que no se mide. Por corrida se mide por separado:

- `strava`: las llamadas a `list_activities` del adaptador HTTPX (simulador en memoria, sin
  red: mide el adaptador, no la latencia de Strava real);
- `transformacion`: `normalize_activity` + `activity_payload` de los registros recibidos;
- `cierre`: la transacción final de la corrida (bloqueo de la corrida, `imported_activity` y
  `actividad.importada.v1` en la outbox, hasta el COMMIT);
- `relevo`: `terceros-outbox-publisher` publicando el lote en el emulador de Pub/Sub. Solo con
  `PUBSUB_EMULATOR_HOST`; sin emulador se informa `relevo=omitido` y la publicación medida es
  solo el COMMIT de la outbox (`cierre`);
- `publicacion` = `cierre` + `relevo`, y `total` = corrida completa (`RunSync`) + `relevo`.

Imprime una línea `EXP-002 ...` con p50/p95/p99 en milisegundos (rango más cercano) y exige
p95 < 2 s en la transformación, la publicación y el total. Sin esperas artificiales.
"""

import base64
import math
import os
import secrets
import time
import unittest
import uuid
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker
from test_sync_flow import (
    ThirdPartyDatabase,
    build_sync,
    new_token_key,
    queue_run,
    seed_account,
    strava_context,
    synthetic_activities,
    third_party_database,
)

from third_party_management.application.imports import activity_payload
from third_party_management.application.ports import (
    ActivityPage,
    ProviderGrant,
    ProviderTokens,
    StravaGateway,
)
from third_party_management.domain.activities import normalize_activity
from third_party_management.domain.synchronization import SyncRun
from third_party_management.infrastructure.messaging.pubsub import PubSubEventPublisher
from third_party_management.infrastructure.persistence.models import OutboxEventRow
from third_party_management.infrastructure.persistence.outbox import SqlAlchemyOutboxStore
from third_party_management.infrastructure.persistence.unit_of_work import SqlAlchemyUnitOfWork
from third_party_management.settings import TERCEROS_TOPIC, OutboxPublisherSettings
from third_party_management.testing.strava_simulator import StravaSimulator
from third_party_management.workers.outbox_publisher import OutboxRelay
from third_party_management.workers.pubsub_setup import ensure_resources

RUNS = 30
ACTIVITIES_PER_RUN = 50
P95_LIMIT_MS = 2000.0
PERCENTILES = (0.50, 0.95, 0.99)


def percentile(values: list[float], fraction: float) -> float:
    """Percentil por rango más cercano."""
    ordered = sorted(values)
    return ordered[max(0, math.ceil(fraction * len(ordered)) - 1)]


def summary(name: str, values: list[float]) -> str:
    parts = " ".join(
        f"p{round(fraction * 100)}={percentile(values, fraction):.1f}" for fraction in PERCENTILES
    )
    return f"{name}[{parts}]"


def _elapsed_ms(started: float) -> float:
    return (time.perf_counter() - started) * 1000


class _TimedGateway:
    """Mide `list_activities` y conserva los registros recibidos en la corrida."""

    def __init__(self, gateway: StravaGateway) -> None:
        self._gateway = gateway
        self.elapsed_ms = 0.0
        self.records: list[object] = []

    def reset(self) -> None:
        self.elapsed_ms, self.records = 0.0, []

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
        started = time.perf_counter()
        result = self._gateway.list_activities(
            access_token, after=after, page=page, per_page=per_page
        )
        self.elapsed_ms += _elapsed_ms(started)
        self.records.extend(result.records)
        return result


class _TimedUnitOfWork(SqlAlchemyUnitOfWork):
    """Mide la transacción final: desde el bloqueo de la corrida hasta su COMMIT."""

    def __init__(self, sessions: sessionmaker[Session], closings: list[float]) -> None:
        super().__init__(sessions)
        self._closings = closings
        self._started: float | None = None

    def lock_owned_run(self, sync_run_id: UUID, worker_id: str) -> SyncRun | None:
        self._started = time.perf_counter()
        return super().lock_owned_run(sync_run_id, worker_id)

    def commit(self) -> None:
        super().commit()
        if self._started is not None:
            self._closings.append(_elapsed_ms(self._started))


class Exp002SimulatorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.db: ThirdPartyDatabase = self.enterContext(third_party_database("tpm_exp002"))
        self.strava = StravaSimulator()
        self.user_id = uuid.uuid4()
        context = strava_context(
            self.strava, base64.urlsafe_b64decode(new_token_key()), secrets.token_hex(16)
        )
        seed_account(
            self.db,
            context,
            self.user_id,
            self.strava.issue_tokens(),
            datetime.now(UTC) + timedelta(hours=6),
        )
        self.gateway = _TimedGateway(context.gateway)
        self.closings: list[float] = []
        sessions = self.db.sessions
        self.run_sync = build_sync(
            self.db,
            replace(context, gateway=self.gateway),
            unit_of_work=lambda: _TimedUnitOfWork(sessions, self.closings),
        )
        self.relay = self._relay()

    def _relay(self) -> Callable[[], int] | None:
        if not os.environ.get("PUBSUB_EMULATOR_HOST"):
            return None
        project = f"sportapp-test-{uuid.uuid4().hex[:8]}"
        ensure_resources(project)
        publisher = PubSubEventPublisher(project, TERCEROS_TOPIC)
        self.addCleanup(publisher.close)
        values: dict[str, Any] = {
            "app_env": "test",
            "database_url": self.db.url,
            "pubsub_project_id": project,
            "worker_id": "terceros-outbox-exp002",
        }
        relay = OutboxRelay(
            SqlAlchemyOutboxStore(self.db.sessions),
            publisher,
            OutboxPublisherSettings(**values),
        )
        return relay.run_once

    def one_run(self, number: int) -> dict[str, float]:
        self.strava.activities = synthetic_activities(
            100_000 + number * ACTIVITIES_PER_RUN, ACTIVITIES_PER_RUN
        )
        self.gateway.reset()
        queue_run(self.db, self.user_id)

        started = time.perf_counter()
        outcome = self.run_sync()
        sync_ms = _elapsed_ms(started)
        assert outcome is not None
        self.assertEqual(
            (outcome.status, outcome.imported_count), ("SUCCEEDED", ACTIVITIES_PER_RUN)
        )

        started = time.perf_counter()
        for record in self.gateway.records:
            activity_payload(normalize_activity(record), self.user_id, 1)
        transform_ms = _elapsed_ms(started)

        relay_ms = 0.0
        if self.relay is not None:
            started = time.perf_counter()
            self.assertEqual(self.relay(), ACTIVITIES_PER_RUN)
            relay_ms = _elapsed_ms(started)
        closing_ms = self.closings[-1]
        return {
            "strava": self.gateway.elapsed_ms,
            "transformacion": transform_ms,
            "cierre": closing_ms,
            "relevo": relay_ms,
            "publicacion": closing_ms + relay_ms,
            "total": sync_ms + relay_ms,
        }

    def test_transformation_and_publication_p95_under_two_seconds(self) -> None:
        """HA012 / EXP-002: p50/p95/p99 de transformación y publicación con el simulador."""
        self.one_run(0)  # Calentamiento (pool, canal de Pub/Sub): no se mide.
        samples = [self.one_run(number) for number in range(1, RUNS + 1)]

        series = {name: [sample[name] for sample in samples] for name in samples[0]}
        relay = summary("relevo", series["relevo"]) if self.relay else "relevo=omitido"
        print(
            f"EXP-002 corridas={RUNS} actividades={ACTIVITIES_PER_RUN} ms "
            f"{summary('strava', series['strava'])} "
            f"{summary('transformacion', series['transformacion'])} "
            f"{summary('cierre', series['cierre'])} {relay} "
            f"{summary('publicacion', series['publicacion'])} "
            f"{summary('total', series['total'])}"
        )

        self.assertEqual(self.db.count(OutboxEventRow), (RUNS + 1) * ACTIVITIES_PER_RUN)
        if self.relay is not None:
            unpublished = [row for row in self.db.rows(OutboxEventRow) if row.published_at is None]
            self.assertEqual(unpublished, [])
        for name in ("transformacion", "publicacion", "total"):
            self.assertLess(percentile(series[name], 0.95), P95_LIMIT_MS, name)


if __name__ == "__main__":
    unittest.main()
