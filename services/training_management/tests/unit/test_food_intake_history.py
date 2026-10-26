"""HU013 sin infraestructura: historial de alimentación propio y del deportista autorizado."""

import unittest
from datetime import date, timedelta
from uuid import UUID, uuid4

from fakes import NOW, FakePermissions, InMemoryAuthorizationReader, InMemoryReadStore
from intake_fakes import InMemoryFoodIntakeReader, stored_intake
from leak_checks import leaks

from training_management.application.errors import (
    AthleteAuthorizationRequired,
    PermissionRequired,
)
from training_management.application.ports import AuthorizationView, Caller, FoodIntakePosition
from training_management.application.queries.food_intakes import (
    MAX_LIMIT,
    FoodIntakeFilters,
    GetFoodIntakeHistory,
    InvalidFoodIntakeQuery,
    decode_cursor,
    encode_cursor,
)
from training_management.domain.nutrition.model import MealType

NUTRITION_HISTORY = "nutrition.history.view_assigned"
PERMISSIONS = FakePermissions(
    {"service_provider": {"nutrition.plan.author", NUTRITION_HISTORY}, "organizer": set()}
)


def authorization(
    athlete_id: UUID, professional_id: UUID, status: str = "ACTIVE"
) -> AuthorizationView:
    return AuthorizationView(
        athlete_id=athlete_id,
        professional_id=professional_id,
        status=status,
        version=1 if status == "ACTIVE" else 2,
        granted_at=NOW - timedelta(days=10),
        revoked_at=NOW - timedelta(days=1) if status != "ACTIVE" else None,
    )


class FoodIntakeHistoryCase(unittest.TestCase):
    def setUp(self) -> None:
        self.read = InMemoryReadStore()
        self.athlete = Caller(uuid4(), "athlete")
        self.professional = Caller(uuid4(), "service_provider")
        self.history = GetFoodIntakeHistory(
            reader=InMemoryFoodIntakeReader(self.read),
            authorizations=InMemoryAuthorizationReader(self.read),
            permissions=PERMISSIONS,
        )

    def add(self, athlete_id: UUID, hours_ago: float, meal: MealType = MealType.BREAKFAST) -> UUID:
        intake = stored_intake(athlete_id, NOW - timedelta(hours=hours_ago), meal)
        self.read.food_intakes[intake.intake_id] = intake
        return intake.intake_id

    def authorize(self, status: str = "ACTIVE") -> None:
        key = (self.athlete.user_id, self.professional.user_id)
        self.read.authorizations[key] = authorization(*key, status=status)


class OwnFoodIntakeHistoryTest(FoodIntakeHistoryCase):
    def test_returns_own_intakes_newest_first(self) -> None:
        """CA1: consumos con alimento, cantidad, unidad, tipo de comida y fecha, descendentes."""
        ids = [self.add(self.athlete.user_id, hours) for hours in (30, 2, 10)]

        page = self.history.own(self.athlete, FoodIntakeFilters())

        self.assertEqual([item.intake_id for item in page.items], [ids[1], ids[2], ids[0]])
        self.assertIsNone(page.next_cursor)
        first = page.items[0]
        self.assertEqual(first.meal_type, MealType.BREAKFAST)
        self.assertEqual(
            [(item.food_code, str(item.quantity), item.unit) for item in first.items],
            [("OATS", "80.0", "G"), ("MILK", "250.0", "ML")],
        )

    def test_empty_history_is_an_explicit_empty_page(self) -> None:
        """CA5: sin consumos, colección vacía y sin cursor."""
        page = self.history.own(self.athlete, FoodIntakeFilters())

        self.assertEqual((page.items, page.next_cursor), ((), None))

    def test_isolates_other_athletes(self) -> None:
        mine = self.add(self.athlete.user_id, 1)
        self.add(uuid4(), 1)

        page = self.history.own(self.athlete, FoodIntakeFilters())

        self.assertEqual([item.intake_id for item in page.items], [mine])

    def test_period_is_inclusive_by_utc_day(self) -> None:
        """CA1: `from` y `to` incluyen el día completo en UTC."""
        today = NOW.date()
        inside = self.add(self.athlete.user_id, 0.5)
        yesterday = self.add(self.athlete.user_id, 24)
        self.add(self.athlete.user_id, 72)

        only_today = self.history.own(self.athlete, FoodIntakeFilters(today, today))
        two_days = self.history.own(
            self.athlete, FoodIntakeFilters(today - timedelta(days=1), today)
        )
        open_start = self.history.own(
            self.athlete, FoodIntakeFilters(date_to=today - timedelta(days=1))
        )

        self.assertEqual([item.intake_id for item in only_today.items], [inside])
        self.assertEqual([item.intake_id for item in two_days.items], [inside, yesterday])
        self.assertEqual(len(open_start.items), 2)

    def test_filters_by_meal_type(self) -> None:
        lunch = self.add(self.athlete.user_id, 3, MealType.LUNCH)
        self.add(self.athlete.user_id, 2, MealType.HYDRATION)

        page = self.history.own(self.athlete, FoodIntakeFilters(meal_type=MealType.LUNCH))

        self.assertEqual([item.intake_id for item in page.items], [lunch])

    def test_paginates_without_losses_or_repeats(self) -> None:
        """CA4: keyset estable incluso con instantes iguales (desempate por `intake_id`)."""
        expected = {self.add(self.athlete.user_id, hours // 2) for hours in range(25)}

        seen: list[UUID] = []
        cursor: str | None = None
        while True:
            page = self.history.own(self.athlete, FoodIntakeFilters(limit=4, cursor=cursor))
            seen.extend(item.intake_id for item in page.items)
            cursor = page.next_cursor
            if cursor is None:
                break

        self.assertEqual(len(seen), len(expected))
        self.assertEqual(set(seen), expected)
        ordered = [self.read.food_intakes[intake_id] for intake_id in seen]
        keys = [(item.consumed_at, item.intake_id) for item in ordered]
        self.assertEqual(keys, sorted(keys, reverse=True))

    def test_invalid_period_limit_or_cursor_is_validation_error(self) -> None:
        """CA2: periodo invertido, límite fuera de rango o cursor corrupto."""
        today = NOW.date()
        cases = (
            ("to", "to_before_from", FoodIntakeFilters(today, today - timedelta(days=1))),
            ("limit", "out_of_range", FoodIntakeFilters(limit=MAX_LIMIT + 1)),
            ("limit", "out_of_range", FoodIntakeFilters(limit=0)),
            ("cursor", "invalid_cursor", FoodIntakeFilters(cursor="no-es-un-cursor")),
        )
        for field, issue, filters in cases:
            with self.subTest(filters=filters), self.assertRaises(InvalidFoodIntakeQuery) as raised:
                self.history.own(self.athlete, filters)
            self.assertEqual((raised.exception.field, raised.exception.issue), (field, issue))
            self.assertEqual(raised.exception.code, "validation_error")

    def test_only_athletes_use_the_own_history(self) -> None:
        for actor in ("service_provider", "organizer"):
            with self.subTest(actor=actor), self.assertRaises(PermissionRequired):
                self.history.own(Caller(uuid4(), actor), FoodIntakeFilters())


class CursorTest(unittest.TestCase):
    def test_round_trip(self) -> None:
        position = FoodIntakePosition(consumed_at=NOW, intake_id=uuid4())

        self.assertEqual(decode_cursor(encode_cursor(position)), position)

    def test_rejects_corrupt_cursors(self) -> None:
        naive = encode_cursor(FoodIntakePosition(NOW.replace(tzinfo=None), uuid4()))
        for cursor in ("@@@", "e30", naive, "eyJjIjoiMjAyNiIsImkiOiJ4In0"):
            with self.subTest(cursor=cursor), self.assertRaises(InvalidFoodIntakeQuery):
                decode_cursor(cursor)


class AthleteFoodIntakeHistoryTest(FoodIntakeHistoryCase):
    def test_authorized_professional_sees_the_athlete_intakes(self) -> None:
        intake_id = self.add(self.athlete.user_id, 1)
        self.add(uuid4(), 1)
        self.authorize()

        page = self.history.of_athlete(self.professional, self.athlete.user_id, FoodIntakeFilters())

        self.assertEqual([item.intake_id for item in page.items], [intake_id])

    def test_denies_without_active_authorization_or_permission(self) -> None:
        """CA3: sin autorización, revocada, de otro profesional o actor sin el permiso."""
        self.add(self.athlete.user_id, 1)
        other = Caller(uuid4(), "service_provider")
        self.authorize("REVOKED")
        self.read.authorizations[(self.athlete.user_id, other.user_id)] = authorization(
            self.athlete.user_id, other.user_id
        )

        with self.assertRaises(AthleteAuthorizationRequired):
            self.history.of_athlete(self.professional, self.athlete.user_id, FoodIntakeFilters())
        with self.assertRaises(AthleteAuthorizationRequired):
            self.history.of_athlete(self.professional, uuid4(), FoodIntakeFilters())
        for actor in ("athlete", "organizer"):
            with self.subTest(actor=actor), self.assertRaises(PermissionRequired):
                self.history.of_athlete(
                    Caller(uuid4(), actor), self.athlete.user_id, FoodIntakeFilters()
                )

    def test_validates_after_authorizing(self) -> None:
        """Sin autorización no se revela si el cursor o el periodo son válidos."""
        with self.assertRaises(AthleteAuthorizationRequired):
            self.history.of_athlete(
                self.professional, self.athlete.user_id, FoodIntakeFilters(cursor="@@@")
            )
        self.authorize()
        with self.assertRaises(InvalidFoodIntakeQuery):
            self.history.of_athlete(
                self.professional, self.athlete.user_id, FoodIntakeFilters(cursor="@@@")
            )

    def test_every_professional_access_is_audited_without_food_data(self) -> None:
        """CA3: concedido y rechazado se auditan; nunca alimentos, cantidades ni filtros."""
        self.add(self.athlete.user_id, 1)
        with self.assertLogs("sportapp.audit", level="INFO") as logs:
            with self.assertRaises(AthleteAuthorizationRequired):
                self.history.of_athlete(
                    self.professional, self.athlete.user_id, FoodIntakeFilters()
                )
            self.authorize()
            self.history.of_athlete(
                self.professional,
                self.athlete.user_id,
                FoodIntakeFilters(meal_type=MealType.BREAKFAST),
            )
            with self.assertRaises(PermissionRequired):
                self.history.of_athlete(
                    Caller(uuid4(), "organizer"), self.athlete.user_id, FoodIntakeFilters()
                )

        self.assertEqual(
            {record.getMessage() for record in logs.records}, {"audit.nutrition_history_access"}
        )
        fields = [record.__dict__["fields"] for record in logs.records]
        self.assertEqual(
            [item["result"] for item in fields],
            [
                "rejected:athlete_authorization_required",
                "granted",
                "rejected:permission_required",
            ],
        )
        for record_fields in fields:
            self.assertEqual(set(record_fields), {"result", "professional_id", "athlete_id"})
            self.assertEqual(record_fields["athlete_id"], str(self.athlete.user_id))
            self.assertEqual(
                leaks(record_fields, ("OATS", "MILK", "80", "250", "BREAKFAST", "Bearer")), []
            )

    def test_own_history_is_not_audited(self) -> None:
        self.add(self.athlete.user_id, 1)

        with self.assertNoLogs("sportapp.audit", level="INFO"):
            self.history.own(self.athlete, FoodIntakeFilters(date_from=date(2026, 10, 1)))


if __name__ == "__main__":
    unittest.main()
