"""Alta gratuita de HU001 (ADR-024): criterios 6, 7 y 9 sin infraestructura."""

import unittest
import uuid
from uuid import UUID

from fakes import (
    FREE_V1,
    FixedClock,
    InMemoryAffiliationStore,
    InMemoryProvisioningUnitOfWork,
    provision_with,
    user_registered,
)

from affiliation_management.application.commands.provision_free_subscription import (
    CatalogUnavailable,
    ProvisioningOutcome,
)
from affiliation_management.application.messages import (
    InvalidEvent,
    RegisteredUser,
    parse_user_registered,
)
from affiliation_management.application.ports import InboxResult
from affiliation_management.domain.subscriptions.model import (
    SubscriptionOrigin,
    SubscriptionStatus,
    receives_free_plan,
)


def event(**overrides: object) -> RegisteredUser:
    return parse_user_registered(user_registered(**overrides))


class ParseUserRegisteredTest(unittest.TestCase):
    def test_extracts_only_what_affiliation_needs(self) -> None:
        envelope = user_registered(actor_type="organizer")

        parsed = parse_user_registered(envelope)

        self.assertEqual(parsed.event_id, UUID(envelope["event_id"]))
        self.assertEqual(parsed.user_id, UUID(envelope["payload"]["user_id"]))
        self.assertEqual(parsed.actor_type, "organizer")
        self.assertEqual(parsed.correlation_id, UUID(envelope["correlation_id"]))
        self.assertNotIn("ana@example.com", repr(parsed))

    def test_rejects_events_outside_the_contract(self) -> None:
        cases = {
            "unexpected_event_type": user_registered(event_type="usuario.actualizado.v1"),
            "unsupported_schema_version": user_registered(schema_version=2),
            "aggregate_id_mismatch": user_registered(aggregate_id=str(uuid.uuid4())),
            "event_id_not_uuid": user_registered(event_id="no-es-uuid"),
            "unknown_actor_type": user_registered(actor_type="admin"),
            "payload_not_object": user_registered(payload=[]),
        }
        for reason, envelope in cases.items():
            with self.subTest(reason=reason), self.assertRaises(InvalidEvent) as error:
                parse_user_registered(envelope)
            self.assertEqual(error.exception.reason, reason)
        with self.assertRaises(InvalidEvent):
            parse_user_registered([1, 2])

    def test_missing_correlation_is_allowed(self) -> None:
        self.assertIsNone(event(correlation_id=None).correlation_id)


class ProvisionFreeSubscriptionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = InMemoryAffiliationStore()
        self.clock = FixedClock()
        self.provision = provision_with(self.store, self.clock)

    def test_athlete_receives_one_active_free_subscription_and_event(self) -> None:
        """CA6."""
        incoming = event()

        outcome = self.provision(incoming)

        self.assertEqual(outcome, ProvisioningOutcome.SUBSCRIPTION_CREATED)
        [subscription] = self.store.subscriptions.values()
        self.assertEqual(subscription.user_id, incoming.user_id)
        self.assertEqual(subscription.plan, FREE_V1)
        self.assertEqual(subscription.status, SubscriptionStatus.ACTIVE)
        self.assertEqual(subscription.origin, SubscriptionOrigin.AUTO_REGISTRATION)
        self.assertEqual(subscription.started_at, self.clock.now())
        [message] = self.store.outbox
        self.assertEqual(message.event_type, "suscripcion.activada.v1")
        self.assertEqual(message.aggregate_type, "user_subscription")
        self.assertEqual(message.aggregate_id, incoming.user_id)
        self.assertEqual(message.aggregate_version, 1)
        self.assertEqual(message.causation_id, str(incoming.event_id))
        self.assertEqual(message.correlation_id, incoming.correlation_id)
        self.assertEqual(
            message.payload,
            {
                "subscription_id": str(subscription.subscription_id),
                "user_id": str(incoming.user_id),
                "plan_code": "FREE",
                "plan_version": 1,
                "catalog_version": "plans-2026-10-11.v1",
                "status": "ACTIVE",
                "origin": "AUTO_REGISTRATION",
                "activated_at": self.clock.now().isoformat(),
            },
        )
        self.assertEqual(
            self.store.inbox[incoming.event_id].result, InboxResult.SUBSCRIPTION_CREATED
        )

    def test_organizer_and_service_provider_get_no_subscription(self) -> None:
        """CA7."""
        for actor in ("organizer", "service_provider"):
            with self.subTest(actor=actor):
                incoming = event(actor_type=actor)

                outcome = self.provision(incoming)

                self.assertEqual(outcome, ProvisioningOutcome.NOT_APPLICABLE)
                self.assertEqual(
                    self.store.inbox[incoming.event_id].result, InboxResult.NOT_APPLICABLE
                )
        self.assertEqual(self.store.subscriptions, {})
        self.assertEqual(self.store.outbox, [])
        self.assertFalse(receives_free_plan("organizer"))

    def test_repeated_delivery_does_not_create_another_subscription(self) -> None:
        """CA9."""
        incoming = event()
        self.provision(incoming)

        outcome = self.provision(incoming)

        self.assertEqual(outcome, ProvisioningOutcome.DUPLICATE)
        self.assertEqual(len(self.store.subscriptions), 1)
        self.assertEqual(len(self.store.outbox), 1)

    def test_other_event_for_an_active_user_is_recorded_without_new_subscription(self) -> None:
        """CA9: un reintento con otro `event_id` tampoco duplica."""
        first = event()
        self.provision(first)
        second = event(user_id=first.user_id)

        outcome = self.provision(second)

        self.assertEqual(outcome, ProvisioningOutcome.ALREADY_ACTIVE)
        self.assertEqual(len(self.store.subscriptions), 1)
        self.assertEqual(self.store.inbox[second.event_id].result, InboxResult.ALREADY_ACTIVE)

    def test_concurrent_activation_for_the_same_user_keeps_one_subscription(self) -> None:
        """La otra transacción confirma primero: esta deja constancia y no duplica."""
        first = event()
        second = event(user_id=first.user_id)
        competing = provision_with(self.store, self.clock)

        def win_race() -> None:
            competing(first)

        provision = provision_with(
            self.store,
            self.clock,
            unit_of_work=lambda: InMemoryProvisioningUnitOfWork(self.store, before_commit=win_race),
        )

        outcome = provision(second)

        self.assertEqual(outcome, ProvisioningOutcome.ALREADY_ACTIVE)
        self.assertEqual(len(self.store.subscriptions), 1)
        self.assertEqual(self.store.inbox[second.event_id].result, InboxResult.ALREADY_ACTIVE)

    def test_concurrent_duplicate_delivery_is_reported_as_duplicate(self) -> None:
        incoming = event()
        competing = provision_with(self.store, self.clock)
        provision = provision_with(
            self.store,
            self.clock,
            unit_of_work=lambda: InMemoryProvisioningUnitOfWork(
                self.store, before_commit=lambda: competing(incoming)
            ),
        )

        self.assertEqual(provision(incoming), ProvisioningOutcome.DUPLICATE)
        self.assertEqual(len(self.store.subscriptions), 1)

    def test_missing_catalog_raises_so_the_event_is_retried(self) -> None:
        self.store.default_plan = None
        incoming = event()

        with self.assertRaises(CatalogUnavailable):
            self.provision(incoming)

        self.assertEqual(self.store.inbox, {}, "sin registrar: el evento debe reintentarse")
        self.assertEqual(self.store.subscriptions, {})

    def test_audit_records_outcome_without_personal_data(self) -> None:
        with self.assertLogs("sportapp.audit", level="INFO") as audit:
            self.provision(event())

        [record] = audit.records
        self.assertEqual(record.fields["result"], "subscription_created")  # type: ignore[attr-defined]
        self.assertNotIn("ana@example.com", str(record.__dict__))
        self.assertNotIn("Ana", str(record.__dict__))


if __name__ == "__main__":
    unittest.main()
