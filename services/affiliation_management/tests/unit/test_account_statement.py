"""HU007 (ADR-036) sin infraestructura: `statement_status`, compromisos listados, mapeo de
movimientos, paginación y API de `GET /v1/me/account-statement`."""

import base64
import json
import logging
import unittest
import uuid
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from fastapi.testclient import TestClient
from subscription_fakes import NOW, OFFER_IDS, VERIFIER, plan_ref, token

from affiliation_management.api.account_statement import build_account_statement_router
from affiliation_management.application.errors import PermissionRequired
from affiliation_management.application.ports import (
    AccountStatementSnapshot,
    Caller,
    MovementPosition,
    MovementView,
    SubscriptionView,
)
from affiliation_management.application.queries.account_statement import (
    GetMyAccountStatement,
    InvalidStatementQuery,
    StatementQuery,
    decode_cursor,
    encode_cursor,
)
from affiliation_management.domain.subscriptions.account_statement import (
    StatementStatus,
    is_listed,
    listed_commitments,
    statement_status,
)
from affiliation_management.domain.subscriptions.change import (
    BillingCommitment,
    CommitmentStatus,
    Movement,
    MovementType,
)
from affiliation_management.domain.subscriptions.model import (
    Periodicity,
    Subscription,
    SubscriptionOrigin,
    SubscriptionStatus,
)
from sportapp_platform.app import create_service_app
from sportapp_platform.config import ServiceSettings

ACTORS = ("athlete", "organizer", "service_provider")


class FixedClock:
    def now(self) -> datetime:
        return NOW


def subscription(user_id: UUID, plan: str = "INTERMEDIATE", version: int = 2) -> Subscription:
    periodicity = Periodicity.NONE if plan == "FREE" else Periodicity.MONTHLY
    return Subscription(
        subscription_id=uuid.uuid4(),
        user_id=user_id,
        plan=plan_ref(plan),
        status=SubscriptionStatus.ACTIVE,
        origin=SubscriptionOrigin.USER_CHANGE,
        started_at=NOW - timedelta(days=1),
        version=version,
        periodicity=periodicity,
        plan_offer_id=OFFER_IDS[(plan, periodicity)],
        period_start=NOW - timedelta(days=1),
        period_end=None if plan == "FREE" else NOW + timedelta(days=29),
    )


def commitment(
    subscription_id: UUID,
    *,
    period_start: datetime,
    status: CommitmentStatus = CommitmentStatus.PENDING_EXTERNAL,
    months: int = 1,
) -> BillingCommitment:
    return BillingCommitment(
        commitment_id=uuid.uuid4(),
        subscription_id=subscription_id,
        user_id=uuid.uuid4(),
        plan_code="INTERMEDIATE",
        plan_version=2,
        plan_offer_id=OFFER_IDS[("INTERMEDIATE", Periodicity.MONTHLY)],
        periodicity=Periodicity.MONTHLY,
        amount=Decimal("2400.00"),
        currency="COP",
        period_start=period_start,
        period_end=period_start + timedelta(days=30 * months),
        due_at=period_start,
        status=status,
        created_at=period_start,
    )


def movement(
    user_id: UUID,
    occurred_at: datetime,
    movement_type: MovementType = MovementType.PLAN_CHANGED,
    commitment_id: UUID | None = None,
) -> Movement:
    return Movement(
        movement_id=uuid.uuid4(),
        user_id=user_id,
        movement_type=movement_type,
        occurred_at=occurred_at,
        subscription_id=uuid.uuid4(),
        plan_code="INTERMEDIATE",
        periodicity=Periodicity.MONTHLY,
        commitment_id=commitment_id,
    )


class InMemoryStatementReader:
    """Aplica el mismo orden y keyset que PostgreSQL y registra las lecturas."""

    def __init__(self) -> None:
        self.current: SubscriptionView | None = None
        self.has_any = False
        self.commitments: list[BillingCommitment] = []
        self.movements: list[MovementView] = []
        self.calls: list[tuple[UUID, MovementPosition | None, int]] = []

    def read(
        self, user_id: UUID, *, after: MovementPosition | None, limit: int
    ) -> AccountStatementSnapshot:
        self.calls.append((user_id, after, limit))
        ordered = sorted(
            self.movements,
            key=lambda view: (view.movement.occurred_at, view.movement.movement_id.int),
            reverse=True,
        )
        if after is not None:
            ordered = [
                view
                for view in ordered
                if (view.movement.occurred_at, view.movement.movement_id.int)
                < (after.occurred_at, after.movement_id.int)
            ]
        return AccountStatementSnapshot(
            current=self.current,
            has_any_subscription=self.has_any or self.current is not None,
            commitments=list(self.commitments),
            movements=ordered[:limit],
        )


class StatementStatusTest(unittest.TestCase):
    def test_the_four_statuses(self) -> None:
        """CA1 y CA2: `ACTIVE`, `PROVISIONING`, `NOT_APPLICABLE` y `NONE`."""
        cases = {
            ("athlete", True, True): StatementStatus.ACTIVE,
            ("athlete", False, False): StatementStatus.PROVISIONING,
            ("athlete", False, True): StatementStatus.NONE,
            ("organizer", False, False): StatementStatus.NOT_APPLICABLE,
            ("service_provider", False, False): StatementStatus.NOT_APPLICABLE,
            # El actor manda: un organizador nunca tiene estado de deportista.
            ("organizer", True, True): StatementStatus.NOT_APPLICABLE,
        }
        for (actor, active, any_subscription), expected in cases.items():
            with self.subTest(actor=actor, active=active, any=any_subscription):
                self.assertIs(
                    statement_status(
                        actor,
                        has_active_subscription=active,
                        has_any_subscription=any_subscription,
                    ),
                    expected,
                )


class ListedCommitmentsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.current_id = uuid.uuid4()
        self.previous_id = uuid.uuid4()

    def test_current_and_running_commitments_in_any_status_are_listed(self) -> None:
        """CA1: vigentes (`PENDING_EXTERNAL`) y condonados (`WAIVED`) cuyo periodo no terminó."""
        current = commitment(self.current_id, period_start=NOW - timedelta(days=1))
        waived = commitment(
            self.previous_id,
            period_start=NOW - timedelta(days=5),
            status=CommitmentStatus.WAIVED,
        )
        pending_previous = commitment(self.previous_id, period_start=NOW - timedelta(days=10))
        closed = commitment(self.previous_id, period_start=NOW - timedelta(days=90))

        listed = listed_commitments(
            [closed, pending_previous, current, waived],
            current_subscription_id=self.current_id,
            now=NOW,
        )

        self.assertEqual(
            [c.commitment_id for c in listed],
            [current.commitment_id, waived.commitment_id, pending_previous.commitment_id],
        )

    def test_commitment_of_the_current_subscription_is_listed_even_if_its_period_ended(
        self,
    ) -> None:
        ended = commitment(self.current_id, period_start=NOW - timedelta(days=60))
        self.assertTrue(is_listed(ended, current_subscription_id=self.current_id, now=NOW))
        self.assertFalse(is_listed(ended, current_subscription_id=None, now=NOW))

    def test_period_ending_exactly_now_is_closed(self) -> None:
        boundary = commitment(self.previous_id, period_start=NOW - timedelta(days=30))
        self.assertFalse(is_listed(boundary, current_subscription_id=self.current_id, now=NOW))


class CursorTest(unittest.TestCase):
    def test_round_trip_keeps_microseconds_and_identifier(self) -> None:
        position = MovementPosition(NOW.replace(microsecond=123456), uuid.uuid4())
        self.assertEqual(decode_cursor(encode_cursor(position)), position)

    def test_invalid_cursors(self) -> None:
        for cursor in ("@@@", "e30", "bm90LWpzb24"):
            with self.subTest(cursor=cursor), self.assertRaises(InvalidStatementQuery) as raised:
                decode_cursor(cursor)
            self.assertEqual(
                (raised.exception.field, raised.exception.issue), ("cursor", "invalid_cursor")
            )

    def test_naive_instant_is_rejected(self) -> None:
        raw = json.dumps({"t": "2026-10-23T15:00:00", "m": str(uuid.uuid4())}).encode()
        cursor = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
        with self.assertRaises(InvalidStatementQuery):
            decode_cursor(cursor)


class GetMyAccountStatementTest(unittest.TestCase):
    def setUp(self) -> None:
        self.reader = InMemoryStatementReader()
        self.query = GetMyAccountStatement(reader=self.reader, clock=FixedClock())
        self.user_id = uuid.uuid4()
        self.athlete = Caller(self.user_id, "athlete")

    def test_active_athlete_receives_subscription_commitments_and_movements(self) -> None:
        """CA1 y CA4: plan, versión, oferta, precio, periodo, compromisos y movimientos."""
        current = subscription(self.user_id)
        self.reader.current = SubscriptionView(current, Decimal("2400"))
        created = commitment(current.subscription_id, period_start=current.period_start or NOW)
        self.reader.commitments = [created]
        self.reader.movements = [
            MovementView(
                movement(self.user_id, NOW - timedelta(days=2), MovementType.FREE_ACTIVATED),
                None,
                None,
            ),
            MovementView(
                movement(
                    self.user_id,
                    NOW - timedelta(days=1),
                    MovementType.COMMITMENT_CREATED,
                    created.commitment_id,
                ),
                Decimal("2400"),
                "COP",
            ),
        ]

        document = self.query(self.athlete, StatementQuery())

        self.assertEqual(document["statement_status"], "ACTIVE")
        self.assertEqual(
            {
                key: document["subscription"][key]
                for key in ("plan_code", "plan_version", "periodicity", "price", "status")
            },
            {
                "plan_code": "INTERMEDIATE",
                "plan_version": 2,
                "periodicity": "MONTHLY",
                "price": "2400.00",
                "status": "ACTIVE",
            },
        )
        self.assertTrue(document["subscription"]["period_end"].endswith("Z"))
        self.assertEqual(
            document["commitments"],
            [
                {
                    "commitment_id": str(created.commitment_id),
                    "plan_code": "INTERMEDIATE",
                    "periodicity": "MONTHLY",
                    "amount": "2400.00",
                    "currency": "COP",
                    "period_start": "2026-10-22T15:00:00Z",
                    "period_end": "2026-11-21T15:00:00Z",
                    "due_at": "2026-10-22T15:00:00Z",
                    "status": "PENDING_EXTERNAL",
                }
            ],
        )
        first, second = document["movements"]
        self.assertEqual(
            (first["movement_type"], first["amount"], first["currency"], first["commitment_id"]),
            ("COMMITMENT_CREATED", "2400.00", "COP", str(created.commitment_id)),
        )
        self.assertEqual(
            (
                second["movement_type"],
                second["amount"],
                second["currency"],
                second["commitment_id"],
            ),
            ("FREE_ACTIVATED", None, None, None),
        )
        self.assertEqual(second["occurred_at"], "2026-10-21T15:00:00Z")
        self.assertEqual(
            (document["payments_status"], document["balance"]), ("NOT_INTEGRATED", None)
        )
        self.assertIsNone(document["next_cursor"])

    def test_every_movement_type_is_mapped(self) -> None:
        self.reader.current = SubscriptionView(subscription(self.user_id), Decimal("2400"))
        self.reader.movements = [
            MovementView(movement(self.user_id, NOW - timedelta(minutes=i), kind), None, None)
            for i, kind in enumerate(MovementType)
        ]

        document = self.query(self.athlete, StatementQuery())

        self.assertEqual(
            [item["movement_type"] for item in document["movements"]],
            [kind.value for kind in MovementType],
        )
        for item in document["movements"]:
            self.assertEqual(item["periodicity"], "MONTHLY")
            self.assertEqual(item["plan_code"], "INTERMEDIATE")

    def test_provisioning_athlete_receives_empty_lists(self) -> None:
        """CA2: deportista sin suscripción → `PROVISIONING`, no un error."""
        document = self.query(self.athlete, StatementQuery())

        self.assertEqual(
            document,
            {
                "statement_status": "PROVISIONING",
                "subscription": None,
                "commitments": [],
                "movements": [],
                "next_cursor": None,
                "payments_status": "NOT_INTEGRATED",
                "balance": None,
            },
        )

    def test_athlete_without_active_subscription_but_history_is_none(self) -> None:
        self.reader.has_any = True
        self.reader.movements = [
            MovementView(movement(self.user_id, NOW, MovementType.FREE_ACTIVATED), None, None)
        ]

        document = self.query(self.athlete, StatementQuery())

        self.assertEqual(document["statement_status"], "NONE")
        self.assertIsNone(document["subscription"])
        self.assertEqual(len(document["movements"]), 1)

    def test_organizer_and_professional_are_not_applicable_without_reading(self) -> None:
        """CA2: `NOT_APPLICABLE` con listas vacías; no se consulta la base."""
        for actor in ("organizer", "service_provider"):
            with self.subTest(actor=actor):
                document = self.query(Caller(self.user_id, actor), StatementQuery())
                self.assertEqual(
                    (document["statement_status"], document["subscription"]),
                    ("NOT_APPLICABLE", None),
                )
                self.assertEqual((document["commitments"], document["movements"]), ([], []))
                self.assertEqual(document["payments_status"], "NOT_INTEGRATED")
        self.assertEqual(self.reader.calls, [])

    def test_unknown_actor_is_rejected(self) -> None:
        with self.assertRaises(PermissionRequired):
            self.query(Caller(self.user_id, "administrator"), StatementQuery())

    def test_reads_only_the_callers_account(self) -> None:
        """CA3: el `user_id` del lector es siempre el del token."""
        self.query(self.athlete, StatementQuery(limit=5))
        self.assertEqual(self.reader.calls, [(self.user_id, None, 6)])

    def test_pages_without_losses_or_repetitions(self) -> None:
        """CA5: 57 movimientos (con instantes repetidos) en páginas de 10."""
        self.reader.current = SubscriptionView(subscription(self.user_id), Decimal("2400"))
        self.reader.movements = [
            MovementView(movement(self.user_id, NOW - timedelta(hours=i // 3)), None, None)
            for i in range(57)
        ]
        seen: list[str] = []
        cursor: str | None = None
        pages = 0
        while True:
            document = self.query(self.athlete, StatementQuery(limit=10, cursor=cursor))
            seen.extend(item["movement_id"] for item in document["movements"])
            pages += 1
            cursor = document["next_cursor"]
            if cursor is None:
                break
        expected = sorted(
            self.reader.movements,
            key=lambda view: (view.movement.occurred_at, view.movement.movement_id.int),
            reverse=True,
        )
        self.assertEqual(seen, [str(view.movement.movement_id) for view in expected])
        self.assertEqual(pages, 6)

    def test_limit_out_of_range_and_bad_cursor(self) -> None:
        for query, field in (
            (StatementQuery(limit=0), "limit"),
            (StatementQuery(limit=101), "limit"),
            (StatementQuery(cursor="@@@"), "cursor"),
        ):
            for actor in ACTORS:
                with self.subTest(query=query, actor=actor):
                    with self.assertRaises(InvalidStatementQuery) as raised:
                        self.query(Caller(self.user_id, actor), query)
                    self.assertEqual(raised.exception.field, field)

    def test_limit_bounds_are_accepted(self) -> None:
        for limit in (1, 100):
            with self.subTest(limit=limit):
                self.query(self.athlete, StatementQuery(limit=limit))


class AccountStatementApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.reader = InMemoryStatementReader()
        app = create_service_app(
            service_name="affiliation-management",
            title="t",
            api_version="0",
            settings=ServiceSettings(app_env="test"),  # type: ignore[call-arg]
            routers=[
                build_account_statement_router(
                    verify_token=VERIFIER.verify,
                    get_statement=GetMyAccountStatement(reader=self.reader, clock=FixedClock()),
                )
            ],
        )
        self.client = TestClient(app)
        self.user_id = uuid.uuid4()

    def get(self, actor: str | None = "athlete", **params: Any) -> Any:
        return self.client.get(
            "/v1/me/account-statement",
            params=params,
            headers={"Authorization": f"Bearer {token(self.user_id, actor)}"},
        )

    def test_three_actors_receive_a_statement(self) -> None:
        self.reader.current = SubscriptionView(subscription(self.user_id, "FREE", 1), Decimal("0"))
        expected = {
            "athlete": "ACTIVE",
            "organizer": "NOT_APPLICABLE",
            "service_provider": "NOT_APPLICABLE",
        }
        for actor, status in expected.items():
            with self.subTest(actor=actor):
                response = self.get(actor)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["statement_status"], status)
                self.assertEqual(response.headers["Cache-Control"], "no-store")
                self.assertEqual(response.json()["balance"], None)
        free = self.get().json()["subscription"]
        self.assertEqual(
            (free["plan_code"], free["price"], free["period_end"]), ("FREE", "0.00", None)
        )

    def test_missing_or_invalid_token_is_401(self) -> None:
        """Seguridad: sin token → `401`."""
        for headers in ({}, {"Authorization": "Bearer otro"}, {"Authorization": "Basic x"}):
            with self.subTest(headers=headers):
                response = self.client.get("/v1/me/account-statement", headers=headers)
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.json()["code"], "invalid_access_token")
        self.assertEqual(self.reader.calls, [])

    def test_token_without_or_with_unknown_actor_is_403(self) -> None:
        for actor in (None, "administrator"):
            with self.subTest(actor=actor):
                response = self.get(actor)
                self.assertEqual(response.status_code, 403)
                self.assertEqual(response.json()["code"], "permission_required")

    def test_no_parameter_selects_another_account(self) -> None:
        """CA3: un `user_id` en la consulta no cambia la cuenta leída."""
        other = uuid.uuid4()
        response = self.get(user_id=str(other))

        self.assertEqual(response.status_code, 200)
        self.assertEqual([call[0] for call in self.reader.calls], [self.user_id])

    def test_invalid_query_is_422_with_details(self) -> None:
        cases = (
            ({"limit": "0"}, "query.limit", "out_of_range"),
            ({"limit": "101"}, "query.limit", "out_of_range"),
            ({"limit": "diez"}, "query.limit", "out_of_range"),
            ({"cursor": "@@@"}, "query.cursor", "invalid_cursor"),
        )
        for params, field, issue in cases:
            with self.subTest(params=params):
                response = self.get(**params)
                self.assertEqual(response.status_code, 422, response.text)
                body = response.json()
                self.assertEqual(body["code"], "validation_error")
                self.assertEqual(body["details"], [{"field": field, "issue": issue}])

    def test_next_cursor_is_accepted(self) -> None:
        self.reader.current = SubscriptionView(subscription(self.user_id), Decimal("2400"))
        self.reader.movements = [
            MovementView(movement(self.user_id, NOW - timedelta(minutes=i)), None, None)
            for i in range(3)
        ]
        first = self.get(limit="2").json()
        second = self.get(limit="2", cursor=first["next_cursor"]).json()

        self.assertEqual(len(first["movements"]), 2)
        self.assertEqual(len(second["movements"]), 1)
        self.assertIsNone(second["next_cursor"])

    def test_amounts_are_not_logged(self) -> None:
        """Datos sensibles: importes y periodicidad del usuario no aparecen en los logs."""
        current = subscription(self.user_id)
        self.reader.current = SubscriptionView(current, Decimal("2400"))
        self.reader.commitments = [
            replace(
                commitment(current.subscription_id, period_start=NOW),
                amount=Decimal("2400.00"),
            )
        ]
        with self.assertLogs(level=logging.DEBUG) as logs:
            logging.getLogger("sportapp.test").debug("inicio")
            response = self.get()
        self.assertEqual(response.status_code, 200)
        self.assertFalse(any("2400" in line for line in logs.output), logs.output)


if __name__ == "__main__":
    unittest.main()
