"""Alta gratuita de HU001 contra PostgreSQL real: atomicidad, idempotencia y concurrencia."""

import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

from jsonschema import Draft202012Validator
from sqlalchemy import select
from sqlalchemy.exc import DataError
from support import AffiliationDatabase, affiliation_database, user_registered

from affiliation_management.application.commands.provision_free_subscription import (
    ProvisioningOutcome,
)
from affiliation_management.application.messages import parse_user_registered
from affiliation_management.application.ports import OutboxMessage
from affiliation_management.domain.catalog import FREE_CAPABILITIES
from affiliation_management.infrastructure.persistence.catalog import (
    SqlAlchemyCapabilitiesReader,
)
from affiliation_management.infrastructure.persistence.models import (
    InboxEventRow,
    OutboxEventRow,
    SubscriptionRow,
)
from affiliation_management.infrastructure.persistence.outbox import SqlAlchemyOutboxStore
from affiliation_management.infrastructure.persistence.unit_of_work import (
    SqlAlchemyProvisioningUnitOfWork,
)
from affiliation_management.workers.outbox_publisher import build_envelope
from sportapp_platform.testing.contracts import repository_root

CONCURRENCY = 8
SCHEMA = (
    repository_root(Path(__file__).resolve())
    / "contracts"
    / "events"
    / "afiliacion"
    / "suscripcion.activada.v1.schema.json"
)


class ProvisioningPersistenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.db: AffiliationDatabase = self.enterContext(affiliation_database("affiliation_hu001"))
        self.provision = self.db.provision()

    def test_subscription_outbox_and_inbox_are_committed_together(self) -> None:
        """CA6."""
        event = parse_user_registered(user_registered())

        outcome = self.provision(event)

        self.assertEqual(outcome, ProvisioningOutcome.SUBSCRIPTION_CREATED)
        with self.db.sessions() as session:
            subscription = session.scalars(select(SubscriptionRow)).one()
            outbox = session.scalars(select(OutboxEventRow)).one()
            inbox = session.get_one(InboxEventRow, event.event_id)
        self.assertEqual(
            (subscription.user_id, subscription.plan_code, subscription.plan_version),
            # Deportista nuevo: versión 2 de `FREE`, la vigente en `plans-2026-10-13.v1`.
            (event.user_id, "FREE", 2),
        )
        self.assertEqual(
            (subscription.status, subscription.origin), ("ACTIVE", "AUTO_REGISTRATION")
        )
        self.assertEqual(outbox.aggregate_id, event.user_id)
        self.assertEqual(outbox.causation_id, str(event.event_id))
        self.assertIsNone(outbox.published_at)
        self.assertEqual(inbox.result, "subscription_created")

    def test_capabilities_reader_returns_the_free_plan(self) -> None:
        event = parse_user_registered(user_registered())
        self.provision(event)

        snapshot = SqlAlchemyCapabilitiesReader(self.db.sessions).active_snapshot(event.user_id)

        assert snapshot is not None
        self.assertEqual(snapshot.plan.plan_code, "FREE")
        self.assertEqual(snapshot.plan.catalog_version, "plans-2026-10-13.v1")
        self.assertEqual(set(snapshot.capabilities), set(FREE_CAPABILITIES))

    def test_non_athletes_only_leave_an_inbox_record(self) -> None:
        """CA7."""
        for actor in ("organizer", "service_provider"):
            self.provision(parse_user_registered(user_registered(actor_type=actor)))

        self.assertEqual(self.db.count(SubscriptionRow), 0)
        self.assertEqual(self.db.count(OutboxEventRow), 0)
        self.assertEqual(self.db.count(InboxEventRow), 2)

    def test_concurrent_deliveries_of_the_same_event_create_one_subscription(self) -> None:
        """CA9: Pub/Sub entrega al menos una vez; el inbox deduplica."""
        event = parse_user_registered(user_registered())

        with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
            outcomes = list(pool.map(lambda _: self.provision(event), range(CONCURRENCY)))

        self.assertEqual(outcomes.count(ProvisioningOutcome.SUBSCRIPTION_CREATED), 1)
        self.assertEqual(outcomes.count(ProvisioningOutcome.DUPLICATE), CONCURRENCY - 1)
        self.assertEqual(self.db.count(SubscriptionRow), 1)
        self.assertEqual(self.db.count(OutboxEventRow), 1)

    def test_concurrent_events_for_one_user_keep_a_single_active_subscription(self) -> None:
        """El índice único parcial impide dos suscripciones activas para el mismo usuario."""
        first = user_registered()
        events = [
            parse_user_registered(user_registered(user_id=first["payload"]["user_id"]))
            for _ in range(CONCURRENCY)
        ]

        with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
            outcomes = list(pool.map(self.provision, events))

        self.assertEqual(outcomes.count(ProvisioningOutcome.SUBSCRIPTION_CREATED), 1)
        self.assertEqual(outcomes.count(ProvisioningOutcome.ALREADY_ACTIVE), CONCURRENCY - 1)
        self.assertEqual(self.db.count(SubscriptionRow), 1)
        self.assertEqual(self.db.count(InboxEventRow), CONCURRENCY)

    def test_failure_writing_the_event_rolls_back_subscription_and_inbox(self) -> None:
        class BrokenOutbox(SqlAlchemyProvisioningUnitOfWork):
            def add_outbox(self, message: OutboxMessage) -> None:
                super().add_outbox(replace(message, event_type="x" * 300))

        provision = self.db.provision(lambda: BrokenOutbox(self.db.sessions))

        with self.assertRaises(DataError):
            provision(parse_user_registered(user_registered()))

        self.assertEqual(self.db.count(SubscriptionRow), 0)
        self.assertEqual(self.db.count(InboxEventRow), 0, "sin inbox: el evento se reintenta")

    def test_outbox_envelope_matches_the_versioned_schema(self) -> None:
        self.provision(parse_user_registered(user_registered()))
        store = SqlAlchemyOutboxStore(self.db.sessions)

        [claimed] = store.claim_batch("w-1", 10, 30)

        Draft202012Validator(json.loads(SCHEMA.read_text(encoding="utf-8"))).validate(
            build_envelope(claimed)
        )
        self.assertEqual(store.claim_batch("w-2", 10, 30), [], "arrendado por w-1")
        store.mark_published(claimed.event_id, "w-1")
        with self.db.sessions() as session:
            self.assertIsNotNone(session.get_one(OutboxEventRow, claimed.event_id).published_at)


if __name__ == "__main__":
    unittest.main()
