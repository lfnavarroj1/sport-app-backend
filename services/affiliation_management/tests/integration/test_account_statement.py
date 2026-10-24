"""HU007 (ADR-036) contra PostgreSQL real: estado de cuenta del deportista `FREE` recién
activado, tras cambios de plan, sin suscripción y con 57 movimientos paginados por keyset
(requiere `TEST_DATABASE_URL`). La consulta no escribe nada."""

import unittest
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select, text
from support import AffiliationDatabase, affiliation_database, user_registered

from affiliation_management.application.commands.change_subscription import (
    ChangeSubscription,
    ChangeSubscriptionCommand,
)
from affiliation_management.application.messages import parse_user_registered
from affiliation_management.application.ports import Caller
from affiliation_management.application.queries.account_statement import (
    GetMyAccountStatement,
    StatementQuery,
)
from affiliation_management.domain.subscriptions.change import ChangeRequest
from affiliation_management.domain.subscriptions.model import Periodicity
from affiliation_management.infrastructure.clock import SystemClock
from affiliation_management.infrastructure.persistence.account_statement import (
    SqlAlchemyAccountStatementReader,
)
from affiliation_management.infrastructure.persistence.models import (
    BillingCommitmentRow,
    CommandIdempotencyRow,
    OutboxEventRow,
    SubscriptionHistoryRow,
    SubscriptionRow,
)
from affiliation_management.infrastructure.persistence.subscriptions import (
    SqlAlchemyCatalogReader,
    SqlAlchemySubscriptionChangeUnitOfWork,
)

MOVEMENTS = 57
WRITE_TABLES = (
    SubscriptionRow,
    BillingCommitmentRow,
    SubscriptionHistoryRow,
    OutboxEventRow,
    CommandIdempotencyRow,
)


class AccountStatementPersistenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self._context = affiliation_database("affiliation_hu007")
        self.db: AffiliationDatabase = self._context.__enter__()
        self.addCleanup(self._context.__exit__, None, None, None)
        self.statement = GetMyAccountStatement(
            reader=SqlAlchemyAccountStatementReader(self.db.sessions), clock=SystemClock()
        )
        self.change = ChangeSubscription(
            unit_of_work=lambda: SqlAlchemySubscriptionChangeUnitOfWork(self.db.sessions),
            catalog=SqlAlchemyCatalogReader(self.db.sessions),
            clock=SystemClock(),
        )

    def registered_athlete(self) -> Caller:
        envelope = user_registered()
        self.db.provision()(parse_user_registered(envelope))
        return Caller(UUID(envelope["payload"]["user_id"]), "athlete")

    def apply(self, caller: Caller, plan: str, periodicity: str, version: int) -> None:
        self.change(
            caller,
            ChangeSubscriptionCommand(
                ChangeRequest(
                    plan_code=plan,
                    periodicity=Periodicity(periodicity),
                    expected_subscription_version=version,
                ),
                uuid.uuid4(),
            ),
        )

    def read(self, caller: Caller, **query: Any) -> dict[str, Any]:
        return self.statement(caller, StatementQuery(**query))

    def counts(self) -> dict[str, int]:
        return {model.__tablename__: self.db.count(model) for model in WRITE_TABLES}

    def test_recently_activated_free_athlete(self) -> None:
        """CA1 y CA4: `FREE` recién activado, sin compromisos y con su `FREE_ACTIVATED`."""
        athlete = self.registered_athlete()

        document = self.read(athlete)

        self.assertEqual(document["statement_status"], "ACTIVE")
        subscription = document["subscription"]
        self.assertEqual(
            (
                subscription["plan_code"],
                subscription["plan_version"],
                subscription["periodicity"],
                subscription["price"],
                subscription["status"],
                subscription["origin"],
                subscription["period_end"],
            ),
            ("FREE", 2, "NONE", "0.00", "ACTIVE", "AUTO_REGISTRATION", None),
        )
        self.assertTrue(subscription["period_start"].endswith("Z"))
        self.assertEqual(document["commitments"], [])
        [movement] = document["movements"]
        self.assertEqual(
            (movement["movement_type"], movement["plan_code"], movement["periodicity"]),
            ("FREE_ACTIVATED", "FREE", "NONE"),
        )
        self.assertEqual((movement["amount"], movement["currency"]), (None, None))
        self.assertEqual(
            (document["payments_status"], document["balance"], document["next_cursor"]),
            ("NOT_INTEGRATED", None, None),
        )

    def test_athlete_after_plan_changes(self) -> None:
        """CA1: tras `FREE` → `INTERMEDIATE` → `PREMIUM` ve ambos compromisos `PENDING_EXTERNAL`
        y, al volver a `FREE`, ambos `WAIVED` con sus movimientos."""
        athlete = self.registered_athlete()
        self.apply(athlete, "INTERMEDIATE", "MONTHLY", 1)
        self.apply(athlete, "PREMIUM", "QUARTERLY", 2)

        paid = self.read(athlete)

        self.assertEqual(paid["statement_status"], "ACTIVE")
        self.assertEqual(
            (paid["subscription"]["plan_code"], paid["subscription"]["price"]),
            ("PREMIUM", "14400.00"),
        )
        self.assertIsNotNone(paid["subscription"]["period_end"])
        self.assertEqual(
            [(c["plan_code"], c["amount"], c["status"]) for c in paid["commitments"]],
            [
                ("PREMIUM", "14400.00", "PENDING_EXTERNAL"),
                ("INTERMEDIATE", "2400.00", "PENDING_EXTERNAL"),
            ],
        )
        premium = paid["commitments"][0]
        self.assertEqual(premium["due_at"], premium["period_start"])
        self.assertEqual(premium["period_start"], paid["subscription"]["period_start"])
        self.assertEqual(premium["period_end"], paid["subscription"]["period_end"])
        self.assertEqual([m["movement_type"] for m in paid["movements"]][-1], "FREE_ACTIVATED")
        created = [m for m in paid["movements"] if m["movement_type"] == "COMMITMENT_CREATED"]
        self.assertEqual(
            sorted((m["amount"], m["currency"]) for m in created),
            [("14400.00", "COP"), ("2400.00", "COP")],
        )

        self.apply(athlete, "FREE", "NONE", 3)
        returned = self.read(athlete)

        self.assertEqual(returned["subscription"]["plan_code"], "FREE")
        self.assertEqual(
            [(c["plan_code"], c["status"]) for c in returned["commitments"]],
            [("PREMIUM", "WAIVED"), ("INTERMEDIATE", "WAIVED")],
        )
        types = [m["movement_type"] for m in returned["movements"]]
        self.assertEqual(
            sorted(types),
            sorted(
                [
                    "FREE_ACTIVATED",
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
        instants = [m["occurred_at"] for m in returned["movements"]]
        self.assertEqual(instants, sorted(instants, reverse=True))

    def test_provisioning_athlete_and_athlete_without_active_subscription(self) -> None:
        """CA2: sin suscripción → `PROVISIONING`; con historial y sin activa → `NONE`."""
        provisioning = self.read(Caller(uuid.uuid4(), "athlete"))
        self.assertEqual(
            (provisioning["statement_status"], provisioning["subscription"]),
            ("PROVISIONING", None),
        )
        self.assertEqual((provisioning["commitments"], provisioning["movements"]), ([], []))

        athlete = self.registered_athlete()
        with self.db.sessions.begin() as session:
            session.execute(
                text(
                    "UPDATE subscription SET status = 'ENDED', ended_at = now() "
                    "WHERE user_id = :user_id"
                ),
                {"user_id": athlete.user_id},
            )
        none = self.read(athlete)
        self.assertEqual((none["statement_status"], none["subscription"]), ("NONE", None))
        self.assertEqual([m["movement_type"] for m in none["movements"]], ["FREE_ACTIVATED"])

    def test_organizer_and_professional_are_not_applicable(self) -> None:
        """CA2: listas vacías aunque el `user_id` tuviera datos de deportista."""
        athlete = self.registered_athlete()
        for actor in ("organizer", "service_provider"):
            with self.subTest(actor=actor):
                document = self.read(Caller(athlete.user_id, actor))
                self.assertEqual(document["statement_status"], "NOT_APPLICABLE")
                self.assertEqual((document["commitments"], document["movements"]), ([], []))

    def test_57_movements_are_paged_by_keyset_without_losses_or_repetitions(self) -> None:
        """CA5: 57 movimientos con instantes repetidos, páginas de 10 y de 7, otro usuario
        intercalado; mismo orden que `occurred_at DESC, movement_id DESC` en SQL."""
        athlete = self.registered_athlete()
        other = self.registered_athlete()
        with self.db.sessions() as session:
            subscription_id = session.scalar(
                select(SubscriptionRow.subscription_id).where(
                    SubscriptionRow.user_id == athlete.user_id
                )
            )
            other_subscription = session.scalar(
                select(SubscriptionRow.subscription_id).where(
                    SubscriptionRow.user_id == other.user_id
                )
            )
        base = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
        rows = [
            SubscriptionHistoryRow(
                movement_id=uuid.uuid4(),
                user_id=user_id,
                movement_type="PLAN_CHANGED",
                # Tres movimientos por instante: el desempate es `movement_id`.
                occurred_at=base + timedelta(minutes=index // 3, microseconds=7),
                subscription_id=subscription,
                commitment_id=None,
                plan_code="FREE",
                periodicity="NONE",
            )
            for user_id, subscription in (
                (athlete.user_id, subscription_id),
                (other.user_id, other_subscription),
            )
            for index in range(MOVEMENTS - 1)  # + el `FREE_ACTIVATED` del alta = 57.
        ]
        with self.db.sessions.begin() as session:
            session.add_all(rows)
        with self.db.sessions() as session:
            expected = [
                str(movement_id)
                for movement_id in session.scalars(
                    select(SubscriptionHistoryRow.movement_id)
                    .where(SubscriptionHistoryRow.user_id == athlete.user_id)
                    .order_by(
                        SubscriptionHistoryRow.occurred_at.desc(),
                        SubscriptionHistoryRow.movement_id.desc(),
                    )
                )
            ]
        self.assertEqual(len(expected), MOVEMENTS)
        before = self.counts()

        for limit, pages in ((10, 6), (7, 9), (100, 1)):
            with self.subTest(limit=limit):
                seen: list[str] = []
                cursor: str | None = None
                read_pages = 0
                while True:
                    document = self.read(athlete, limit=limit, cursor=cursor)
                    self.assertLessEqual(len(document["movements"]), limit)
                    seen.extend(m["movement_id"] for m in document["movements"])
                    read_pages += 1
                    cursor = document["next_cursor"]
                    if cursor is None:
                        break
                self.assertEqual(seen, expected)
                self.assertEqual(read_pages, pages)
        # La consulta no produce efectos.
        self.assertEqual(self.counts(), before)


if __name__ == "__main__":
    unittest.main()
