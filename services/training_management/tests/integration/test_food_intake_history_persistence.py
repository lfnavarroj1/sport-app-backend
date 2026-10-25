"""HU013 contra PostgreSQL real: historial de alimentación sobre `food_intake_view` proyectada
desde `consumo_alimento.registrado.v1`, keyset con 57 consumos, filtros y autorización vigente,
revocada y ajena con auditoría (ADR-037, CA1-CA5)."""

import unittest
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from support import athlete_and_professional, training_databases

from training_management.application.errors import (
    AthleteAuthorizationRequired,
    PermissionRequired,
)
from training_management.application.messages import parse_training_event
from training_management.application.ports import Caller
from training_management.application.queries.food_intakes import (
    FoodIntakeFilters,
    GetFoodIntakeHistory,
    InvalidFoodIntakeQuery,
)
from training_management.domain.nutrition.catalog import FOOD_CATALOG_VERSION
from training_management.domain.nutrition.intake import FoodIntake, IntakeItem
from training_management.domain.nutrition.intake_events import food_intake_registered_payload
from training_management.domain.nutrition.model import MealType
from training_management.infrastructure.persistence_read.history import (
    SqlAlchemyAuthorizationReader,
)
from training_management.infrastructure.persistence_read.nutrition import (
    SqlAlchemyFoodIntakeReader,
)
from training_management.infrastructure.persistence_write.outbox import SqlAlchemyOutboxStore
from training_management.settings import OutboxPublisherSettings
from training_management.workers.outbox_publisher import OutboxRelay


class NutritionHistoryViewer:
    def allows(self, actor_type: str, permission: str) -> bool:
        return actor_type == "service_provider" and permission == "nutrition.history.view_assigned"


def intake(athlete_id: UUID, consumed_at: datetime, meal: MealType = MealType.LUNCH) -> FoodIntake:
    return FoodIntake(
        intake_id=uuid4(),
        athlete_id=athlete_id,
        consumed_at=consumed_at,
        meal_type=meal,
        items=(
            IntakeItem(food_code="WHITE_RICE", quantity=Decimal("150.0"), unit="G"),
            IntakeItem(food_code="WATER", quantity=Decimal("500.0"), unit="ML"),
        ),
        catalog_version=FOOD_CATALOG_VERSION,
        registered_at=consumed_at + timedelta(minutes=2),
    )


def intake_envelope(item: FoodIntake) -> dict[str, Any]:
    return {
        "event_id": str(uuid4()),
        "event_type": "consumo_alimento.registrado.v1",
        "occurred_at": item.registered_at.isoformat(),
        "producer": "training-management",
        "aggregate_type": "food_intake",
        "aggregate_id": str(item.intake_id),
        "aggregate_version": 1,
        "correlation_id": str(uuid4()),
        "causation_id": str(uuid4()),
        "schema_version": 1,
        "payload": food_intake_registered_payload(item),
    }


class FoodIntakeHistoryPersistenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.db = self.enterContext(training_databases("hu013"))
        self.athlete, self.professional = athlete_and_professional()
        self.history = GetFoodIntakeHistory(
            reader=SqlAlchemyFoodIntakeReader(self.db.reads),
            authorizations=SqlAlchemyAuthorizationReader(self.db.reads),
            permissions=NutritionHistoryViewer(),
        )

    def project(self, items: list[FoodIntake]) -> None:
        project = self.db.project()
        for item in items:
            project(parse_training_event(intake_envelope(item)))

    def project_authorizations(self) -> None:
        """Lleva los eventos de autorización de HU010/HU011 del outbox a la base de lectura."""
        sent: list[dict[str, Any]] = []

        class Capture:
            def publish(self, envelope: dict[str, Any], ordering_key: str) -> None:
                sent.append(envelope)

        OutboxRelay(
            SqlAlchemyOutboxStore(self.db.writes),
            Capture(),
            OutboxPublisherSettings(
                app_env="test",  # type: ignore[arg-type]
                database_write_url=self.db.write_url,  # type: ignore[arg-type]
                pubsub_project_id="sportapp-test",
                worker_id="w-test",
            ),
        ).run_once()
        project = self.db.project()
        for envelope in sent:
            project(parse_training_event(envelope))

    def test_keyset_over_57_intakes_without_losses_or_repeats(self) -> None:
        """CA1 y CA4 con el índice y el keyset reales: 50 instantes distintos y 7 empatados."""
        start = datetime(2026, 10, 1, 6, 0, tzinfo=UTC)
        tie = start + timedelta(days=2)
        mine = [intake(self.athlete.user_id, start + timedelta(hours=i)) for i in range(50)]
        mine += [intake(self.athlete.user_id, tie, MealType.HYDRATION) for _ in range(7)]
        foreign = intake(uuid4(), start)
        self.project([*mine, foreign])

        seen: list[UUID] = []
        cursor: str | None = None
        while True:
            page = self.history.own(self.athlete, FoodIntakeFilters(limit=20, cursor=cursor))
            keys = [(item.consumed_at, item.intake_id) for item in page.items]
            self.assertEqual(keys, sorted(keys, reverse=True))
            seen.extend(item.intake_id for item in page.items)
            if page.next_cursor is None:
                break
            cursor = page.next_cursor

        self.assertEqual(len(seen), 57)
        self.assertEqual(len(seen), len(set(seen)), "consumo repetido entre páginas")
        self.assertEqual(set(seen), {item.intake_id for item in mine}, "consumo perdido o ajeno")
        first = self.history.own(self.athlete, FoodIntakeFilters(limit=1)).items[0]
        self.assertEqual(
            [(i.food_code, str(i.quantity), i.unit) for i in first.items],
            [("WHITE_RICE", "150.0", "G"), ("WATER", "500.0", "ML")],
        )

    def test_filters_period_meal_type_and_empty_history(self) -> None:
        """CA1, CA2 y CA5: periodo inclusivo por día UTC, tipo de comida y colección vacía."""
        empty = self.history.own(self.athlete, FoodIntakeFilters())
        day = datetime(2026, 10, 5, tzinfo=UTC)
        inside = [
            intake(self.athlete.user_id, day, MealType.BREAKFAST),
            intake(self.athlete.user_id, day + timedelta(hours=23, minutes=59), MealType.DINNER),
        ]
        outside = [
            intake(self.athlete.user_id, day - timedelta(seconds=1)),
            intake(self.athlete.user_id, day + timedelta(days=1)),
        ]
        self.project([*inside, *outside])

        one_day = self.history.own(
            self.athlete, FoodIntakeFilters(date(2026, 10, 5), date(2026, 10, 5))
        )
        dinner = self.history.own(
            self.athlete,
            FoodIntakeFilters(date(2026, 10, 5), date(2026, 10, 5), MealType.DINNER),
        )

        self.assertEqual((empty.items, empty.next_cursor), ((), None))
        self.assertEqual(
            [item.intake_id for item in one_day.items],
            [inside[1].intake_id, inside[0].intake_id],
        )
        self.assertEqual([item.intake_id for item in dinner.items], [inside[1].intake_id])
        with self.assertRaises(InvalidFoodIntakeQuery):
            self.history.own(self.athlete, FoodIntakeFilters(date(2026, 10, 6), date(2026, 10, 5)))
        with self.assertRaises(InvalidFoodIntakeQuery):
            self.history.own(self.athlete, FoodIntakeFilters(cursor="corrupto"))

    def test_active_revoked_and_foreign_authorizations(self) -> None:
        """CA3: vigente concede; ajena y revocada → `403`; cada intento se audita."""
        self.project([intake(self.athlete.user_id, datetime(2026, 10, 3, 12, tzinfo=UTC))])
        stranger = Caller(uuid4(), "service_provider")
        self.db.grant()(self.athlete, self.professional.user_id)
        self.project_authorizations()

        with self.assertLogs("sportapp.audit", level="INFO") as logs:
            granted = self.history.of_athlete(
                self.professional, self.athlete.user_id, FoodIntakeFilters()
            )
            with self.assertRaises(AthleteAuthorizationRequired):
                self.history.of_athlete(stranger, self.athlete.user_id, FoodIntakeFilters())
            with self.assertRaises(PermissionRequired):
                self.history.of_athlete(
                    Caller(uuid4(), "organizer"), self.athlete.user_id, FoodIntakeFilters()
                )
            self.db.revoke()(self.athlete, self.professional.user_id)
            self.project_authorizations()
            with self.assertRaises(AthleteAuthorizationRequired):
                self.history.of_athlete(
                    self.professional, self.athlete.user_id, FoodIntakeFilters()
                )

        self.assertEqual(len(granted.items), 1)
        results = [
            record.__dict__["fields"]["result"]
            for record in logs.records
            if record.getMessage() == "audit.nutrition_history_access"
        ]
        self.assertEqual(
            results,
            [
                "granted",
                "rejected:athlete_authorization_required",
                "rejected:permission_required",
                "rejected:athlete_authorization_required",
            ],
        )


if __name__ == "__main__":
    unittest.main()
