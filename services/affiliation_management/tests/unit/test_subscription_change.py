"""HU016 (ADR-036) sin infraestructura: reglas del cambio de plan, comando, consultas y API."""

import logging
import unittest
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from fastapi.testclient import TestClient
from subscription_fakes import (
    CATALOG,
    NOW,
    OFFER_IDS,
    VERIFIER,
    InMemoryCatalog,
    InMemoryChangeUnitOfWork,
    InMemorySubscriptionReader,
    InMemorySubscriptionStore,
    token,
)

from affiliation_management.api.subscriptions import build_subscriptions_router
from affiliation_management.application.commands.change_subscription import (
    ChangeSubscription,
    ChangeSubscriptionCommand,
)
from affiliation_management.application.errors import IdempotencyKeyReused, PermissionRequired
from affiliation_management.application.ports import Caller
from affiliation_management.application.queries.subscriptions import GetMySubscription, ListPlans
from affiliation_management.domain.subscriptions.change import (
    ChangeRequest,
    CommitmentStatus,
    InconsistentPeriodicity,
    MovementType,
    PlanNotFound,
    PlanNotSelectable,
    SubscriptionProvisioning,
    SubscriptionUnchanged,
    SubscriptionVersionConflict,
    add_months,
)
from affiliation_management.domain.subscriptions.model import Periodicity, SubscriptionStatus
from sportapp_platform.app import create_service_app
from sportapp_platform.config import ServiceSettings


class FixedClock:
    def __init__(self) -> None:
        self.current = NOW

    def now(self) -> datetime:
        return self.current


def request(plan: str, periodicity: str, version: int = 1) -> ChangeRequest:
    return ChangeRequest(
        plan_code=plan, periodicity=Periodicity(periodicity), expected_subscription_version=version
    )


class PeriodTest(unittest.TestCase):
    def test_months_are_calendar_months_clamped_to_the_month_end(self) -> None:
        start = datetime(2026, 1, 31, 10, tzinfo=UTC)
        self.assertEqual(add_months(start, 1), datetime(2026, 2, 28, 10, tzinfo=UTC))
        self.assertEqual(add_months(start, 3), datetime(2026, 4, 30, 10, tzinfo=UTC))
        self.assertEqual(add_months(start, 12), datetime(2027, 1, 31, 10, tzinfo=UTC))
        self.assertEqual(
            add_months(datetime(2027, 11, 29, tzinfo=UTC), 3), datetime(2028, 2, 29, tzinfo=UTC)
        )


class ChangeSubscriptionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = InMemorySubscriptionStore()
        self.catalog = InMemoryCatalog()
        self.clock = FixedClock()
        self.change = ChangeSubscription(
            unit_of_work=lambda: InMemoryChangeUnitOfWork(self.store),
            catalog=self.catalog,
            clock=self.clock,
        )
        self.athlete = Caller(uuid.uuid4(), "athlete")
        self.free = self.store.free_subscription(self.athlete.user_id)

    def run_change(self, req: ChangeRequest, key: UUID | None = None) -> dict[str, Any]:
        result = self.change(self.athlete, ChangeSubscriptionCommand(req, key or uuid.uuid4()))
        return result.document

    def test_free_to_paid_ends_previous_and_creates_one_active_with_commitment(self) -> None:
        """CA1 y CA6: una sola `ACTIVE`, oferta, periodo y compromiso `PENDING_EXTERNAL`."""
        document = self.run_change(request("PREMIUM", "QUARTERLY"))

        self.assertEqual(
            (document["plan_code"], document["periodicity"], document["price"]),
            ("PREMIUM", "QUARTERLY", "14400.00"),
        )
        self.assertEqual(
            (document["status"], document["origin"], document["version"]),
            ("ACTIVE", "USER_CHANGE", 2),
        )
        self.assertEqual(document["period_start"], "2026-10-23T15:00:00Z")
        self.assertEqual(document["period_end"], "2027-01-23T15:00:00Z")
        previous = self.store.subscriptions[self.free.subscription_id]
        self.assertIs(previous.status, SubscriptionStatus.ENDED)
        self.assertEqual(
            [s.status for s in self.store.subscriptions.values()].count(SubscriptionStatus.ACTIVE),
            1,
        )
        [commitment] = self.store.commitments.values()
        self.assertEqual(
            (commitment.amount, commitment.status, commitment.due_at, commitment.period_end),
            (
                self.catalog.offer("PREMIUM", Periodicity.QUARTERLY).price,
                CommitmentStatus.PENDING_EXTERNAL,
                NOW,
                datetime(2027, 1, 23, 15, tzinfo=UTC),
            ),
        )
        self.assertEqual(
            [m.movement_type for m in self.store.movements],
            [MovementType.PLAN_CHANGED, MovementType.COMMITMENT_CREATED],
        )

    def test_event_has_identifiers_plans_offer_and_period(self) -> None:
        """CA5: `suscripcion.cambiada.v1` sin datos personales ni importes."""
        document = self.run_change(request("INTERMEDIATE", "MONTHLY"))

        [message] = self.store.outbox
        self.assertEqual(
            (
                message.event_type,
                message.aggregate_type,
                message.aggregate_id,
                message.aggregate_version,
            ),
            ("suscripcion.cambiada.v1", "user_subscription", self.athlete.user_id, 2),
        )
        self.assertEqual(
            message.payload,
            {
                "subscription_id": document["subscription_id"],
                "previous_subscription_id": str(self.free.subscription_id),
                "user_id": str(self.athlete.user_id),
                "previous_plan_code": "FREE",
                "plan_code": "INTERMEDIATE",
                "plan_version": 2,
                "periodicity": "MONTHLY",
                "catalog_version": CATALOG,
                "status": "ACTIVE",
                "origin": "USER_CHANGE",
                "period_start": "2026-10-23T15:00:00Z",
                "period_end": "2026-11-23T15:00:00Z",
                "changed_at": "2026-10-23T15:00:00Z",
            },
        )

    def test_paid_to_paid_keeps_and_return_to_free_waives_current_commitments(self) -> None:
        """Decisión del 2026-10-23: pago→pago conserva; volver a `FREE` condona los vigentes."""
        self.run_change(request("INTERMEDIATE", "ANNUAL"))
        self.clock.current = NOW + timedelta(days=1)
        self.run_change(request("PREMIUM", "MONTHLY", version=2))
        pending = [
            c
            for c in self.store.commitments.values()
            if c.status is CommitmentStatus.PENDING_EXTERNAL
        ]
        self.assertEqual(len(pending), 2)

        self.clock.current = NOW + timedelta(days=40)  # el mensual ya terminó; el anual no
        document = self.run_change(request("FREE", "NONE", version=3))

        statuses = {c.plan_code: c.status for c in self.store.commitments.values()}
        self.assertEqual(
            statuses,
            {"INTERMEDIATE": CommitmentStatus.WAIVED, "PREMIUM": CommitmentStatus.PENDING_EXTERNAL},
        )
        self.assertEqual(
            (document["plan_code"], document["price"], document["period_end"]),
            ("FREE", "0.00", None),
        )
        self.assertEqual(
            [m.movement_type for m in self.store.movements][-2:],
            [MovementType.RETURNED_TO_FREE, MovementType.COMMITMENT_WAIVED],
        )

    def test_eligibility_rejections_leave_the_subscription_untouched(self) -> None:
        """CA2: plan inexistente, no seleccionable o periodicidad incoherente."""
        self.catalog.selectable["INTERMEDIATE"] = False
        self.catalog.removed.add(("PREMIUM", Periodicity.ANNUAL))
        cases = (
            (request("GOLD", "MONTHLY"), PlanNotFound),
            (request("INTERMEDIATE", "MONTHLY"), PlanNotSelectable),
            (request("PREMIUM", "ANNUAL"), PlanNotSelectable),
            (request("PREMIUM", "NONE"), InconsistentPeriodicity),
            (request("FREE", "MONTHLY"), InconsistentPeriodicity),
        )
        for req, error in cases:
            with (
                self.subTest(plan=req.plan_code, periodicity=req.periodicity),
                self.assertRaises(error),
            ):
                self.run_change(req)
        self.assertEqual(self.store.commits, 0)
        self.assertIs(self.store.active(self.athlete.user_id), self.free)

    def test_state_rejections(self) -> None:
        """CA3/CA4: mismo plan → `subscription_unchanged`; versión vieja → `version_conflict`;
        sin suscripción → `subscription_provisioning`."""
        with self.assertRaises(SubscriptionUnchanged):
            self.run_change(request("FREE", "NONE"))
        with self.assertRaises(SubscriptionVersionConflict) as conflict:
            self.run_change(request("PREMIUM", "MONTHLY", version=7))
        self.assertEqual(conflict.exception.current_version, 1)
        newcomer = Caller(uuid.uuid4(), "athlete")
        with self.assertRaises(SubscriptionProvisioning):
            self.change(
                newcomer, ChangeSubscriptionCommand(request("PREMIUM", "MONTHLY"), uuid.uuid4())
            )
        self.assertEqual(self.store.commits, 0)

    def test_idempotent_retry_and_reused_key(self) -> None:
        """CA3: misma clave y payload → respuesta original sin duplicar; otro payload → 409."""
        key = uuid.uuid4()
        first = self.change(
            self.athlete, ChangeSubscriptionCommand(request("PREMIUM", "MONTHLY"), key)
        )
        retry = self.change(
            self.athlete, ChangeSubscriptionCommand(request("PREMIUM", "MONTHLY"), key)
        )

        self.assertEqual((retry.document, retry.replayed), (first.document, True))
        self.assertEqual(len(self.store.outbox), 1)
        with self.assertRaises(IdempotencyKeyReused):
            self.change(
                self.athlete, ChangeSubscriptionCommand(request("INTERMEDIATE", "MONTHLY"), key)
            )

    def test_concurrent_change_wins_and_the_other_gets_version_conflict(self) -> None:
        """CA4: otra transacción confirma un cambio en medio → `version_conflict`."""

        def concurrent() -> None:
            self.change(
                self.athlete,
                ChangeSubscriptionCommand(request("INTERMEDIATE", "MONTHLY"), uuid.uuid4()),
            )

        change = ChangeSubscription(
            unit_of_work=lambda: InMemoryChangeUnitOfWork(self.store, before_commit=concurrent),
            catalog=self.catalog,
            clock=self.clock,
        )
        with self.assertRaises(SubscriptionVersionConflict) as conflict:
            change(
                self.athlete, ChangeSubscriptionCommand(request("PREMIUM", "MONTHLY"), uuid.uuid4())
            )

        self.assertEqual(conflict.exception.current_version, 2)
        active = self.store.active(self.athlete.user_id)
        assert active is not None
        self.assertEqual(active.plan.plan_code, "INTERMEDIATE")

    def test_only_athletes_and_audit_without_amounts(self) -> None:
        with self.assertRaises(PermissionRequired):
            self.change(
                Caller(uuid.uuid4(), "organizer"),
                ChangeSubscriptionCommand(request("PREMIUM", "MONTHLY"), uuid.uuid4()),
            )
        with self.assertLogs("sportapp.audit", level="INFO") as audit:
            self.run_change(request("PREMIUM", "ANNUAL"))

        [record] = audit.records
        fields = record.fields  # type: ignore[attr-defined]
        self.assertEqual(
            (
                fields["result"],
                fields["plan_code"],
                fields["previous_plan_code"],
                fields["periodicity"],
            ),
            ("changed", "PREMIUM", "FREE", "ANNUAL"),
        )
        self.assertNotIn("57600", str(fields))


class SubscriptionApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = InMemorySubscriptionStore()
        catalog = InMemoryCatalog()
        reader = InMemorySubscriptionReader(self.store)
        app = create_service_app(
            service_name="affiliation-management",
            title="t",
            api_version="0",
            settings=ServiceSettings(app_env="test"),  # type: ignore[call-arg]
            routers=[
                build_subscriptions_router(
                    verify_token=VERIFIER.verify,
                    list_plans=ListPlans(
                        catalog=catalog, subscriptions=reader, catalog_version=CATALOG
                    ),
                    get_subscription=GetMySubscription(reader),
                    change_subscription=ChangeSubscription(
                        unit_of_work=lambda: InMemoryChangeUnitOfWork(self.store),
                        catalog=catalog,
                        clock=FixedClock(),
                    ),
                )
            ],
        )
        self.client = TestClient(app)
        self.user_id = uuid.uuid4()

    def auth(self, actor: str | None = "athlete", **extra: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token(self.user_id, actor)}", **extra}

    def post(
        self, body: dict[str, Any], key: str | None = None, actor: str | None = "athlete"
    ) -> Any:
        headers = self.auth(actor, **{"Idempotency-Key": key or str(uuid.uuid4())})
        return self.client.post("/v1/me/subscription/changes", json=body, headers=headers)

    def test_new_athlete_sees_free_as_current_plan_and_the_offers(self) -> None:
        """CA7: tras el alta automática `FREE` es el plan actual sin selección manual."""
        self.store.free_subscription(self.user_id)

        response = self.client.get("/v1/plans", headers=self.auth())

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual((body["catalog_version"], body["current_plan_code"]), (CATALOG, "FREE"))
        premium = next(plan for plan in body["plans"] if plan["plan_code"] == "PREMIUM")
        self.assertEqual(
            [(o["periodicity"], o["price"]) for o in premium["offers"]],
            [("MONTHLY", "4800.00"), ("QUARTERLY", "14400.00"), ("ANNUAL", "57600.00")],
        )
        self.assertIn("services.sports_medicine.access", premium["capabilities"])
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_subscription_or_provisioning(self) -> None:
        provisioning = self.client.get("/v1/me/subscription", headers=self.auth()).json()
        self.store.free_subscription(self.user_id)
        active = self.client.get("/v1/me/subscription", headers=self.auth()).json()

        self.assertEqual(provisioning["status"], "PROVISIONING")
        self.assertTrue(all(v is None for k, v in provisioning.items() if k != "status"))
        self.assertEqual(
            (active["plan_code"], active["periodicity"], active["price"], active["version"]),
            ("FREE", "NONE", "0.00", 1),
        )

    def test_change_and_its_errors(self) -> None:
        self.store.free_subscription(self.user_id)
        key = str(uuid.uuid4())
        body = {
            "plan_code": "PREMIUM",
            "periodicity": "MONTHLY",
            "expected_subscription_version": 1,
        }

        changed = self.post(body, key)
        replay = self.post(body, key)
        stale = self.post({**body, "plan_code": "INTERMEDIATE"})
        unchanged = self.post({**body, "expected_subscription_version": 2})
        unknown = self.post({**body, "plan_code": "GOLD", "expected_subscription_version": 2})
        incoherent = self.post({**body, "periodicity": "NONE", "expected_subscription_version": 2})
        shape = self.post({"plan_code": "PREMIUM", "periodicity": "WEEKLY", "user_id": "x"})

        self.assertEqual(changed.status_code, 200, changed.text)
        self.assertEqual(
            (replay.json(), replay.headers["Idempotent-Replayed"]), (changed.json(), "true")
        )
        self.assertEqual(
            (stale.status_code, stale.json()["code"], stale.json()["details"]),
            (
                409,
                "version_conflict",
                [{"field": "body.expected_subscription_version", "issue": "2"}],
            ),
        )
        self.assertEqual(
            (unchanged.status_code, unchanged.json()["code"]), (409, "subscription_unchanged")
        )
        self.assertEqual((unknown.status_code, unknown.json()["code"]), (404, "plan_not_found"))
        self.assertEqual(
            (incoherent.status_code, incoherent.json()["details"]),
            (422, [{"field": "body.periodicity", "issue": "inconsistent_values"}]),
        )
        self.assertEqual((shape.status_code, shape.json()["code"]), (422, "validation_error"))

    def test_provisioning_and_authorization(self) -> None:
        """`PROVISIONING` es recuperable; otros actores `403`; sin token `401`."""
        body = {
            "plan_code": "PREMIUM",
            "periodicity": "MONTHLY",
            "expected_subscription_version": 1,
        }
        provisioning = self.post(body)
        organizer = self.post(body, actor="organizer")
        plans_as_provider = self.client.get("/v1/plans", headers=self.auth("service_provider"))
        anonymous = self.client.get("/v1/me/subscription")

        self.assertEqual(
            (provisioning.status_code, provisioning.json()["code"]),
            (409, "subscription_provisioning"),
        )
        for response in (organizer, plans_as_provider):
            self.assertEqual(
                (response.status_code, response.json()["code"]), (403, "permission_required")
            )
        self.assertEqual(anonymous.status_code, 401)

    def test_logs_never_contain_amounts(self) -> None:
        self.store.free_subscription(self.user_id)
        with self.assertLogs(logging.getLogger(), level="DEBUG") as logs:
            self.post(
                {
                    "plan_code": "PREMIUM",
                    "periodicity": "ANNUAL",
                    "expected_subscription_version": 1,
                }
            )
        rendered = " ".join(logs.output) + " ".join(
            str(getattr(r, "fields", "")) for r in logs.records
        )
        self.assertNotIn("57600", rendered)
        self.assertNotIn(str(OFFER_IDS[("PREMIUM", Periodicity.ANNUAL)]), rendered)


if __name__ == "__main__":
    unittest.main()
