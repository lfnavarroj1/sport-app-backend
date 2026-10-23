"""HU016 (ADR-036) contra PostgreSQL real: ciclo de cambio de plan, concurrencia,
idempotencia, migración `0004_subscription_changes` y `FREE_ACTIVATED` desde el consumidor
(requiere `TEST_DATABASE_URL`). La disponibilidad con ofertas incompletas la cubre
`test_database.CatalogReadinessTest`."""

import json
import threading
import unittest
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from jsonschema import Draft202012Validator, FormatChecker
from sqlalchemy import create_engine, pool, select, text
from support import CATALOG, HISTORY, AffiliationDatabase, affiliation_database, user_registered

from affiliation_management.application.commands.change_subscription import (
    ChangeResult,
    ChangeSubscription,
    ChangeSubscriptionCommand,
)
from affiliation_management.application.messages import parse_user_registered
from affiliation_management.application.ports import Caller
from affiliation_management.domain.subscriptions.change import (
    ChangeRequest,
    SubscriptionVersionConflict,
    free_activated_movement_id,
)
from affiliation_management.domain.subscriptions.model import Periodicity
from affiliation_management.infrastructure.clock import SystemClock
from affiliation_management.infrastructure.persistence.models import (
    BillingCommitmentRow,
    OutboxEventRow,
    SubscriptionHistoryRow,
    SubscriptionRow,
)
from affiliation_management.infrastructure.persistence.outbox import ClaimedEvent
from affiliation_management.infrastructure.persistence.subscriptions import (
    SqlAlchemyCatalogReader,
    SqlAlchemySubscriptionChangeUnitOfWork,
    SqlAlchemySubscriptionReader,
)
from affiliation_management.workers.outbox_publisher import build_envelope
from sportapp_platform.migrations import current_revision, downgrade, upgrade
from sportapp_platform.testing.contracts import repository_root
from sportapp_platform.testing.postgres import temporary_database

CONCURRENT_REQUESTS = 8
SCHEMA = (
    repository_root(Path(__file__).resolve())
    / "contracts/events/afiliacion/suscripcion.cambiada.v1.schema.json"
)


def run_concurrently(calls: list[Callable[[], Any]]) -> list[Any]:
    barrier = threading.Barrier(len(calls))
    outcomes: list[Any] = [None] * len(calls)

    def worker(index: int) -> None:
        barrier.wait()
        try:
            outcomes[index] = calls[index]()
        except Exception as error:  # La prueba clasifica cada resultado.
            outcomes[index] = error

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(len(calls))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    return outcomes


def request(plan: str, periodicity: str, version: int) -> ChangeRequest:
    return ChangeRequest(
        plan_code=plan, periodicity=Periodicity(periodicity), expected_subscription_version=version
    )


class SubscriptionChangePersistenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self._context = affiliation_database("affiliation_hu016")
        self.db: AffiliationDatabase = self._context.__enter__()
        self.addCleanup(self._context.__exit__, None, None, None)
        self.change = ChangeSubscription(
            unit_of_work=lambda: SqlAlchemySubscriptionChangeUnitOfWork(self.db.sessions),
            catalog=SqlAlchemyCatalogReader(self.db.sessions),
            clock=SystemClock(),
        )
        self.athlete = Caller(self.registered_athlete(), "athlete")

    def registered_athlete(self) -> UUID:
        envelope = user_registered()
        self.db.provision()(parse_user_registered(envelope))
        return UUID(envelope["payload"]["user_id"])

    def apply(self, req: ChangeRequest, key: UUID | None = None) -> ChangeResult:
        return self.change(self.athlete, ChangeSubscriptionCommand(req, key or uuid.uuid4()))

    def rows(self, model: Any) -> list[Any]:
        with self.db.sessions() as session:
            return list(session.scalars(select(model)))

    def test_free_intermediate_premium_free_cycle(self) -> None:
        """CA1, CA5, CA6 y CA7: una sola activa en cada paso, compromisos, historial y eventos."""
        reader = SqlAlchemySubscriptionReader(self.db.sessions)
        initial = reader.active(self.athlete.user_id)
        assert initial is not None
        self.assertEqual(
            (initial.subscription.plan.plan_code, initial.subscription.periodicity, initial.price),
            ("FREE", Periodicity.NONE, 0),
        )

        self.apply(request("INTERMEDIATE", "MONTHLY", 1))
        self.apply(request("PREMIUM", "ANNUAL", 2))
        final = self.apply(request("FREE", "NONE", 3)).document

        subscriptions = sorted(self.rows(SubscriptionRow), key=lambda row: row.version)
        self.assertEqual(
            [(s.plan_code, s.status, s.version) for s in subscriptions],
            [
                ("FREE", "ENDED", 1),
                ("INTERMEDIATE", "ENDED", 2),
                ("PREMIUM", "ENDED", 3),
                ("FREE", "ACTIVE", 4),
            ],
        )
        self.assertEqual(
            (final["plan_code"], final["version"], final["price"]), ("FREE", 4, "0.00")
        )
        commitments = {c.plan_code: c for c in self.rows(BillingCommitmentRow)}
        self.assertEqual(
            {code: (c.status, str(c.amount)) for code, c in commitments.items()},
            {"INTERMEDIATE": ("WAIVED", "2400.00"), "PREMIUM": ("WAIVED", "57600.00")},
        )
        with self.db.sessions() as session:
            movements = session.scalars(
                select(SubscriptionHistoryRow.movement_type)
                .where(SubscriptionHistoryRow.user_id == self.athlete.user_id)
                .order_by(SubscriptionHistoryRow.occurred_at, SubscriptionHistoryRow.movement_type)
            ).all()
        self.assertEqual(movements[0], "FREE_ACTIVATED")
        self.assertEqual(
            sorted(movements[1:]),
            sorted(
                [
                    "PLAN_CHANGED",
                    "COMMITMENT_CREATED",
                    "PLAN_CHANGED",
                    "COMMITMENT_CREATED",
                    "RETURNED_TO_FREE",
                    "COMMITMENT_WAIVED",
                    "COMMITMENT_WAIVED",
                ]
            ),
        )
        validator = Draft202012Validator(
            json.loads(SCHEMA.read_text(encoding="utf-8")), format_checker=FormatChecker()
        )
        with self.db.sessions() as session:
            events = session.scalars(
                select(OutboxEventRow)
                .where(OutboxEventRow.event_type == "suscripcion.cambiada.v1")
                .order_by(OutboxEventRow.aggregate_version)
            ).all()
            claimed = [_claimed(event) for event in events]
        self.assertEqual([e.aggregate_version for e in claimed], [2, 3, 4])
        for event in claimed:
            validator.validate(json.loads(json.dumps(build_envelope(event))))

    def test_eight_concurrent_changes_apply_one(self) -> None:
        """CA4: ocho cambios con la misma versión esperada → uno aplicado y siete `409`."""
        plans = [("INTERMEDIATE", "MONTHLY"), ("PREMIUM", "QUARTERLY")]
        outcomes = run_concurrently(
            [lambda i=i: self.apply(request(*plans[i % 2], 1)) for i in range(CONCURRENT_REQUESTS)]
        )

        applied = [o for o in outcomes if isinstance(o, ChangeResult)]
        conflicts = [o for o in outcomes if isinstance(o, SubscriptionVersionConflict)]
        self.assertEqual((len(applied), len(conflicts)), (1, CONCURRENT_REQUESTS - 1), outcomes)
        self.assertTrue(all(conflict.current_version == 2 for conflict in conflicts))
        active = [s for s in self.rows(SubscriptionRow) if s.status == "ACTIVE"]
        self.assertEqual(len(active), 1)
        self.assertEqual(len(self.rows(BillingCommitmentRow)), 1)

    def test_concurrent_retries_with_the_same_key_produce_one_effect(self) -> None:
        """CA3: reintentos simultáneos con la misma clave → mismo resultado y un evento."""
        key = uuid.uuid4()
        outcomes = run_concurrently(
            [
                lambda: self.apply(request("PREMIUM", "MONTHLY", 1), key)
                for _ in range(CONCURRENT_REQUESTS)
            ]
        )

        self.assertTrue(all(isinstance(o, ChangeResult) for o in outcomes), outcomes)
        self.assertEqual(
            {o.document["subscription_id"] for o in outcomes},
            {outcomes[0].document["subscription_id"]},
        )
        self.assertEqual(sum(not o.replayed for o in outcomes), 1)
        with self.db.sessions() as session:
            changed = session.scalars(
                select(OutboxEventRow).where(OutboxEventRow.event_type == "suscripcion.cambiada.v1")
            ).all()
        self.assertEqual(len(changed), 1)

    def test_consumer_registers_free_activated_with_the_none_offer(self) -> None:
        [subscription] = self.rows(SubscriptionRow)
        [movement] = self.rows(SubscriptionHistoryRow)

        self.assertEqual((subscription.periodicity, subscription.period_end), ("NONE", None))
        self.assertIsNotNone(subscription.plan_offer_id)
        self.assertEqual(
            (movement.movement_type, movement.movement_id),
            ("FREE_ACTIVATED", free_activated_movement_id(subscription.subscription_id)),
        )


class SubscriptionChangesMigrationTest(unittest.TestCase):
    def test_upgrade_from_0003_backfills_existing_subscriptions_and_is_repeatable(self) -> None:
        """Línea base `0003` con siete ofertas y suscripciones previas; `0004` repetida."""
        with temporary_database("affiliation_0004") as url:
            upgrade(HISTORY, url, "0003_plan_price_offers")
            engine = create_engine(url, poolclass=pool.NullPool)
            self.addCleanup(engine.dispose)
            started = datetime(2026, 10, 20, 8, tzinfo=UTC)
            subscriptions = {version: uuid.uuid4() for version in (1, 2)}
            with engine.begin() as connection:
                offers = connection.scalar(text("SELECT count(*) FROM plan_offer"))
                for plan_version, subscription_id in subscriptions.items():
                    connection.execute(
                        text(
                            "INSERT INTO subscription (subscription_id, user_id, plan_code, "
                            "plan_version, status, origin, started_at, version, created_at, "
                            "updated_at) VALUES (:id, :user, 'FREE', :pv, 'ACTIVE', "
                            "'AUTO_REGISTRATION', :at, 1, :at, :at)"
                        ),
                        {
                            "id": subscription_id,
                            "user": uuid.uuid4(),
                            "pv": plan_version,
                            "at": started,
                        },
                    )
            self.assertEqual(offers, 7)

            upgrade(HISTORY, url)
            downgrade(HISTORY, url, "0003_plan_price_offers")
            upgrade(HISTORY, url)

            self.assertEqual(current_revision(url), "0004_subscription_changes")
            with engine.connect() as connection:
                rows = {
                    row.subscription_id: row
                    for row in connection.execute(
                        text(
                            "SELECT subscription_id, plan_version, periodicity, period_start, "
                            "plan_offer_id FROM subscription"
                        )
                    )
                }
                movements = connection.execute(
                    text("SELECT movement_id, subscription_id FROM subscription_history")
                ).all()
                catalog = connection.scalar(
                    text("SELECT catalog_version FROM catalog_metadata WHERE catalog_version = :v"),
                    {"v": CATALOG},
                )
            for plan_version, subscription_id in subscriptions.items():
                row = rows[subscription_id]
                self.assertEqual((row.periodicity, row.period_start), ("NONE", started))
                # `FREE` v1 no tiene ofertas; `FREE` v2 recibe la oferta `NONE`.
                self.assertEqual(row.plan_offer_id is None, plan_version == 1)
            self.assertEqual(
                sorted((m.subscription_id, m.movement_id) for m in movements),
                sorted((sid, free_activated_movement_id(sid)) for sid in subscriptions.values()),
            )
            self.assertEqual(catalog, CATALOG)


def _claimed(event: OutboxEventRow) -> ClaimedEvent:
    return ClaimedEvent(
        event_id=event.event_id,
        event_type=event.event_type,
        schema_version=event.schema_version,
        aggregate_type=event.aggregate_type,
        aggregate_id=event.aggregate_id,
        aggregate_version=event.aggregate_version,
        occurred_at=event.occurred_at,
        correlation_id=event.correlation_id,
        causation_id=event.causation_id,
        payload=event.payload,
        attempts=0,
    )


if __name__ == "__main__":
    unittest.main()
