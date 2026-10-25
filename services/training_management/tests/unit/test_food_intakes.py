"""HU043 sin infraestructura: reglas, comando, evento y proyección (ADR-037)."""

import json
import unittest
import uuid
from datetime import timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from fakes import NOW, FixedClock, InMemoryProjectionUnitOfWork, InMemoryReadStore
from intake_fakes import (
    InMemoryFoodIntakeStore,
    InMemoryFoodIntakeUnitOfWork,
    intake_body,
    intake_item,
)
from nutrition_fakes import StaticFoodCatalog
from test_workers import Message

from training_management.application.commands.register_food_intake import (
    OPERATION,
    IntakeOutcome,
    RegisterFoodIntake,
    RegisterFoodIntakeCommand,
)
from training_management.application.errors import (
    IdempotencyKeyReused,
    IntakeIdReused,
    PermissionRequired,
)
from training_management.application.intake_documents import intake_from_document
from training_management.application.messages import (
    FoodIntakeRegisteredEvent,
    InvalidEvent,
    parse_training_event,
)
from training_management.application.ports import Caller
from training_management.application.projections.plan_projection import ProjectionOutcome
from training_management.application.projections.snapshot_projection import TrainingProjector
from training_management.domain.nutrition.catalog import FOOD_CATALOG, FOOD_CATALOG_VERSION
from training_management.domain.nutrition.intake import (
    FoodIntake,
    InvalidFoodIntake,
    validate_food_intake,
)
from training_management.domain.nutrition.intake_events import (
    food_intake_document,
    food_intake_registered_payload,
)
from training_management.workers.read_projector import ProjectionMessageHandler


def issues(body: Any) -> set[tuple[str | None, str]]:
    with unittest.TestCase().assertRaises(InvalidFoodIntake) as caught:
        validate_food_intake(body, FOOD_CATALOG, NOW)
    return {(item.field, item.issue.value) for item in caught.exception.violations}


class FoodIntakeRulesTest(unittest.TestCase):
    """CA1 y CA2: catálogo, unidades por alimento, decimales, límites de ítems y fechas."""

    def test_valid_intake_is_normalized(self) -> None:
        body = intake_body(items=[intake_item("OATS", "80"), intake_item("WATER", "500.0", "ML")])

        data = validate_food_intake(body, FOOD_CATALOG, NOW)

        self.assertEqual(data.intake_id, UUID(body["intake_id"]))
        self.assertEqual(data.consumed_at.isoformat(), "2026-10-12T07:30:00+00:00")
        self.assertEqual(
            [(item.food_code, item.quantity, item.unit) for item in data.items],
            [("OATS", Decimal("80.0"), "G"), ("WATER", Decimal("500.0"), "ML")],
        )

    def test_quantity_bounds_and_format(self) -> None:
        for quantity in ("0.1", "5000.0", "5000", "7.5"):
            with self.subTest(quantity=quantity):
                validate_food_intake(
                    intake_body(items=[intake_item(quantity=quantity)]), FOOD_CATALOG, NOW
                )
        for quantity in ("0", "0.0", "0.05", "5000.1", "10000", "80.25", "-1", "1e3", " 80", 80):
            with self.subTest(quantity=quantity):
                self.assertEqual(
                    issues(intake_body(items=[intake_item(quantity=quantity)])),  # type: ignore[arg-type]
                    {("items[0].quantity", "out_of_range")},
                )

    def test_catalog_and_units(self) -> None:
        self.assertEqual(
            issues(intake_body(items=[intake_item("PIZZA")])),
            {("items[0].food_code", "unknown_value")},
        )
        # El agua solo admite ML y CUP; la avena no admite UNIT.
        self.assertEqual(
            issues(
                intake_body(
                    items=[intake_item("WATER", "1", "G"), intake_item("OATS", "1", "UNIT")]
                )
            ),
            {("items[0].unit", "unit_not_allowed"), ("items[1].unit", "unit_not_allowed")},
        )
        self.assertEqual(
            issues(intake_body(items=[intake_item("OATS", "1", "KG")])),
            {("items[0].unit", "unit_not_allowed")},
        )

    def test_item_limits_and_shape(self) -> None:
        self.assertEqual(issues(intake_body(items=[])), {("items", "too_short")})
        self.assertEqual(issues(intake_body(items=[intake_item()] * 21)), {("items", "too_long")})
        validate_food_intake(intake_body(items=[intake_item()] * 20), FOOD_CATALOG, NOW)
        self.assertEqual(issues(intake_body(items=None)), {("items", "required")})
        self.assertEqual(
            issues(intake_body(items=[{"food_code": "OATS", "unit": "G", "notes": "x"}])),
            {("items[0].notes", "field_not_allowed"), ("items[0].quantity", "required")},
        )
        self.assertEqual(issues(intake_body(items=["OATS"])), {("items[0]", "required")})

    def test_dates(self) -> None:
        validate_food_intake(intake_body(consumed_at="2026-10-12T12:05:00Z"), FOOD_CATALOG, NOW)
        validate_food_intake(intake_body(consumed_at="2026-09-12T12:00:00Z"), FOOD_CATALOG, NOW)
        for value, issue in (
            ("2026-10-12T12:05:01Z", "too_far_in_future"),
            ("2026-09-12T11:59:59Z", "too_old"),
            ("2026-10-12T07:30:00+00:00", "invalid_datetime"),
            ("2026-10-12T07:30:00.123Z", "invalid_datetime"),
            ("2026-02-30T07:30:00Z", "invalid_datetime"),
            (1760254200, "invalid_datetime"),
        ):
            with self.subTest(value=value):
                self.assertEqual(issues(intake_body(consumed_at=value)), {("consumed_at", issue)})
        self.assertEqual(issues(intake_body(consumed_at=None)), {("consumed_at", "required")})

    def test_identifier_meal_type_and_extra_fields(self) -> None:
        self.assertEqual(
            issues(intake_body(intake_id=str(uuid.uuid1()))), {("intake_id", "invalid_uuid")}
        )
        self.assertEqual(issues(intake_body(intake_id="x")), {("intake_id", "invalid_uuid")})
        self.assertEqual(issues(intake_body(meal_type="BRUNCH")), {("meal_type", "unknown_value")})
        self.assertEqual(
            issues(intake_body(athlete_id=str(uuid4()), calories=300)),
            {("athlete_id", "field_not_allowed"), ("calories", "field_not_allowed")},
        )
        self.assertEqual(issues([]), {(None, "required")})


class CommandCase(unittest.TestCase):
    def setUp(self) -> None:
        self.store = InMemoryFoodIntakeStore()
        self.clock = FixedClock()
        self.athlete = Caller(uuid4(), "athlete")
        self.register = self.command()

    def command(self, before_commit: Any = None) -> RegisterFoodIntake:
        return RegisterFoodIntake(
            unit_of_work=lambda: InMemoryFoodIntakeUnitOfWork(self.store, before_commit),
            catalog=StaticFoodCatalog(),
            clock=self.clock,
        )

    def run_command(
        self, body: Any, key: UUID | str | None = None, caller: Caller | None = None
    ) -> Any:
        raw_key = str(key) if key is not None else str(uuid4())
        return self.register(
            caller or self.athlete, RegisterFoodIntakeCommand(body=body, idempotency_key=raw_key)
        )


class RegisterFoodIntakeTest(CommandCase):
    def test_intake_items_event_and_key_are_committed_together(self) -> None:
        """CA1 y CA4: consumo normalizado en versión 1, evento completo y clave idempotente."""
        body, key = intake_body(), uuid4()

        result = self.run_command(body, key)

        intake = result.intake
        self.assertEqual(result.outcome, IntakeOutcome.CREATED)
        self.assertEqual((intake.version, intake.athlete_id), (1, self.athlete.user_id))
        self.assertEqual(intake.catalog_version, FOOD_CATALOG_VERSION)
        self.assertEqual(intake.registered_at, NOW)
        self.assertEqual(list(self.store.intakes), [UUID(body["intake_id"])])
        [message] = self.store.outbox
        self.assertEqual(
            (message.event_type, message.aggregate_type, message.aggregate_version),
            ("consumo_alimento.registrado.v1", "food_intake", 1),
        )
        self.assertEqual(message.aggregate_id, intake.intake_id)
        self.assertEqual(message.causation_id, str(key))
        self.assertEqual(
            message.payload["items"],
            [
                {"food_code": "OATS", "quantity": "80.0", "unit": "G"},
                {"food_code": "BANANA", "quantity": "1.0", "unit": "UNIT"},
                {"food_code": "MILK", "quantity": "250.0", "unit": "ML"},
            ],
        )
        [record] = self.store.commands.values()
        self.assertEqual((record.operation, record.key), (OPERATION, key))
        self.assertEqual(record.expires_at - record.created_at, timedelta(days=30))
        self.assertEqual(record.response, food_intake_document(intake))

    def test_same_key_and_payload_returns_the_original_response(self) -> None:
        """CA3: la misma clave devuelve la respuesta original sin duplicar."""
        body, key = intake_body(), uuid4()
        first = self.run_command(body, key)
        self.clock.advance(timedelta(minutes=3))

        replay = self.run_command(
            {**body, "items": [dict(reversed(i.items())) for i in body["items"]]}, key
        )

        self.assertEqual(replay.outcome, IntakeOutcome.REPLAYED)
        self.assertEqual(replay.intake, first.intake)
        self.assertEqual((len(self.store.intakes), len(self.store.outbox)), (1, 1))

    def test_same_key_with_other_payload_is_rejected(self) -> None:
        body, key = intake_body(), uuid4()
        self.run_command(body, key)

        with self.assertRaises(IdempotencyKeyReused):
            self.run_command({**body, "meal_type": "LUNCH"}, key)
        with self.assertRaises(IdempotencyKeyReused):
            self.run_command(intake_body(), key)
        self.assertEqual(len(self.store.outbox), 1)

    def test_keys_are_isolated_by_athlete_and_expire_after_30_days(self) -> None:
        key = uuid4()
        self.run_command(intake_body(), key)
        other = Caller(uuid4(), "athlete")

        self.run_command(intake_body(), key, caller=other)
        self.clock.advance(timedelta(days=30))
        later = self.run_command(intake_body(consumed_at="2026-11-11T07:30:00Z"), key)

        self.assertEqual(later.outcome, IntakeOutcome.CREATED)
        self.assertEqual(len(self.store.outbox), 3)

    def test_same_intake_id_with_same_data_returns_existing_without_event(self) -> None:
        """CA3: mismo `intake_id` y datos iguales con otra clave → consumo existente."""
        body = intake_body()
        first = self.run_command(body)

        again = self.run_command(
            {
                **body,
                "items": [
                    intake_item(),
                    intake_item("BANANA", "1.0", "UNIT"),
                    intake_item("MILK", "250", "ML"),
                ],
            },
            uuid4(),
        )

        self.assertEqual(again.outcome, IntakeOutcome.EXISTING)
        self.assertEqual(again.intake, first.intake)
        self.assertEqual((len(self.store.outbox), len(self.store.commands)), (1, 1))

    def test_same_intake_id_with_other_data_or_athlete_is_rejected(self) -> None:
        """CA3: datos distintos → `intake_id_reused`; nunca se revela el consumo ajeno."""
        body = intake_body()
        self.run_command(body)
        other = Caller(uuid4(), "athlete")

        for changed, caller in (
            ({**body, "consumed_at": "2026-10-12T07:31:00Z"}, self.athlete),
            ({**body, "meal_type": "SNACK"}, self.athlete),
            ({**body, "items": [intake_item(quantity="81")]}, self.athlete),
            (body, other),
        ):
            with self.subTest(caller=caller.user_id), self.assertRaises(IntakeIdReused):
                self.run_command(changed, caller=caller)
        self.assertEqual(len(self.store.outbox), 1)

    def test_concurrent_identical_requests_return_the_winner(self) -> None:
        """Carrera con la misma clave: quien pierde devuelve lo que confirmó el ganador."""
        body, key = intake_body(), uuid4()
        winner = self.command()
        loser = self.command(
            before_commit=lambda: winner(
                self.athlete, RegisterFoodIntakeCommand(body=body, idempotency_key=str(key))
            )
        )

        result = loser(self.athlete, RegisterFoodIntakeCommand(body, str(key)))

        self.assertEqual(result.outcome, IntakeOutcome.REPLAYED)
        self.assertEqual((len(self.store.intakes), len(self.store.outbox)), (1, 1))

    def test_concurrent_same_intake_id_with_other_key(self) -> None:
        body = intake_body()
        winner = self.command()
        loser = self.command(
            before_commit=lambda: winner(
                self.athlete, RegisterFoodIntakeCommand(body, str(uuid4()))
            )
        )

        result = loser(self.athlete, RegisterFoodIntakeCommand(body, str(uuid4())))

        self.assertEqual(result.outcome, IntakeOutcome.EXISTING)
        self.assertEqual(len(self.store.outbox), 1)

    def test_invalid_requests_persist_nothing(self) -> None:
        """CA2: sin consumo, clave ni evento parcial."""
        for body, key in (
            (intake_body(items=[intake_item("PIZZA")]), str(uuid4())),
            (intake_body(items=[intake_item()] * 21), str(uuid4())),
            (intake_body(consumed_at="2026-01-01T00:00:00Z"), str(uuid4())),
            (intake_body(athlete_id=str(uuid4())), str(uuid4())),
            (intake_body(), None),
            (intake_body(), "not-a-uuid"),
        ):
            with self.subTest(key=key), self.assertRaises(InvalidFoodIntake):
                self.register(self.athlete, RegisterFoodIntakeCommand(body, key))
        self.assertEqual((self.store.intakes, self.store.outbox, self.store.commands), ({}, [], {}))

    def test_missing_key_reports_body_rules_too(self) -> None:
        with self.assertRaises(InvalidFoodIntake) as caught:
            self.register(self.athlete, RegisterFoodIntakeCommand(intake_body(meal_type="X"), " "))

        self.assertEqual(
            {(item.field, item.issue.value) for item in caught.exception.violations},
            {("Idempotency-Key", "required"), ("meal_type", "unknown_value")},
        )

    def test_only_athletes_register_intakes(self) -> None:
        """Seguridad: otro actor → `permission_required` sin leer el catálogo ni persistir."""
        for actor in ("service_provider", "organizer", "admin"):
            with self.subTest(actor=actor), self.assertRaises(PermissionRequired):
                self.run_command(intake_body(), caller=Caller(uuid4(), actor))
        self.assertEqual((self.store.intakes, self.store.outbox), ({}, []))

    def test_audit_has_no_foods_quantities_or_token(self) -> None:
        body = intake_body()
        with self.assertLogs("sportapp.audit", level="INFO") as logs:
            self.run_command(body)
            with self.assertRaises(InvalidFoodIntake):
                self.run_command(intake_body(items=[intake_item(quantity="9999")]))

        fields = [record.__dict__["fields"] for record in logs.records]
        self.assertEqual(
            fields[0],
            {
                "result": "created",
                "intake_id": body["intake_id"],
                "athlete_id": str(self.athlete.user_id),
            },
        )
        self.assertEqual(
            fields[1],
            {
                "result": "rejected",
                "reason": "validation_error",
                "athlete_id": str(self.athlete.user_id),
            },
        )
        self.assertEqual(
            {record.getMessage() for record in logs.records}, {"audit.food_intake_created"}
        )
        allowed = {"result", "reason", "intake_id", "athlete_id"}
        for record_fields in fields:
            self.assertLessEqual(set(record_fields), allowed, "sin alimentos, cantidades ni token")
        # Los identificadores son UUID aleatorios y pueden contener cifras: se excluyen.
        rendered = json.dumps(
            [
                {
                    key: value
                    for key, value in item.items()
                    if key not in {"intake_id", "athlete_id"}
                }
                for item in fields
            ]
        )
        for sensitive in ("OATS", "BANANA", "MILK", "80", "250", "9999", "BREAKFAST", "Bearer"):
            self.assertNotIn(sensitive, rendered)


def an_intake(**overrides: Any) -> FoodIntake:
    data = validate_food_intake(intake_body(**overrides), FOOD_CATALOG, NOW)
    return FoodIntake.register(athlete_id=uuid4(), data=data, catalog=FOOD_CATALOG, now=NOW)


def intake_envelope(intake: FoodIntake, **overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "event_id": str(uuid.uuid4()),
        "event_type": "consumo_alimento.registrado.v1",
        "occurred_at": intake.registered_at.isoformat(),
        "producer": "training-management",
        "aggregate_type": "food_intake",
        "aggregate_id": str(intake.intake_id),
        "aggregate_version": 1,
        "correlation_id": str(uuid.uuid4()),
        "causation_id": str(uuid.uuid4()),
        "schema_version": 1,
        "payload": food_intake_registered_payload(intake),
    }
    values.update(overrides)
    return values


class FoodIntakeEventTest(unittest.TestCase):
    def test_document_round_trip(self) -> None:
        intake = an_intake()

        self.assertEqual(intake_from_document(food_intake_document(intake)), intake)

    def test_valid_event_rebuilds_the_intake(self) -> None:
        intake = an_intake()

        event = parse_training_event(intake_envelope(intake))

        assert isinstance(event, FoodIntakeRegisteredEvent)
        self.assertEqual(event.intake, intake)
        self.assertEqual(event.aggregate_id, intake.intake_id)

    def test_contract_violations(self) -> None:
        intake = an_intake()
        payload = food_intake_registered_payload(intake)
        item = payload["items"][0]
        for overrides in (
            {"aggregate_id": str(uuid4())},
            {"aggregate_version": 2},
            {"schema_version": 2},
            {"payload": {**payload, "meal_type": "BRUNCH"}},
            {"payload": {**payload, "items": []}},
            {"payload": {**payload, "items": [{**item, "quantity": "80"}]}},
            {"payload": {**payload, "items": [{**item, "quantity": "0.0"}]}},
            {"payload": {**payload, "items": [{**item, "unit": "KG"}]}},
            {"payload": {**payload, "items": [{**item, "food_code": "avena"}]}},
            {"payload": {**payload, "version": 2}},
            {"payload": {**payload, "consumed_at": "2026-10-12T07:30:00"}},
            {"payload": {**payload, "intake_id": "x"}},
            {"payload": {**payload, "catalog_version": ""}},
        ):
            with self.subTest(overrides=overrides), self.assertRaises(InvalidEvent):
                parse_training_event(intake_envelope(intake, **overrides))


class FoodIntakeProjectionTest(unittest.TestCase):
    """CA4: `food_intake_view` incorpora el consumo una sola vez."""

    def setUp(self) -> None:
        self.read = InMemoryReadStore()
        self.project = TrainingProjector(
            unit_of_work=lambda: InMemoryProjectionUnitOfWork(self.read), clock=FixedClock()
        )

    def event(self, intake: FoodIntake) -> FoodIntakeRegisteredEvent:
        parsed = parse_training_event(intake_envelope(intake))
        assert isinstance(parsed, FoodIntakeRegisteredEvent)
        return parsed

    def test_projects_once_and_ignores_duplicates(self) -> None:
        intake = an_intake()
        event = self.event(intake)

        outcomes = [self.project(event), self.project(event), self.project(self.event(intake))]

        self.assertEqual(
            outcomes,
            [ProjectionOutcome.PROJECTED, ProjectionOutcome.DUPLICATE, ProjectionOutcome.STALE],
        )
        self.assertEqual(self.read.food_intakes, {intake.intake_id: intake})
        self.assertEqual(len(self.read.processed), 2)

    def test_handler_acks_projected_and_nacks_invalid(self) -> None:
        handler = ProjectionMessageHandler(self.project)
        intake = an_intake()
        ok = Message(intake_envelope(intake))
        invalid = Message(intake_envelope(intake, aggregate_version=2))

        handler(ok)
        with self.assertLogs("sportapp.projector", level="WARNING"):
            handler(invalid)

        self.assertEqual((ok.acked, invalid.acked, invalid.nacked), (True, False, True))
        self.assertEqual(list(self.read.food_intakes), [intake.intake_id])


if __name__ == "__main__":
    unittest.main()
