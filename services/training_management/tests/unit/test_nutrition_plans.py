"""HU014: casos de uso del plan de alimentación con dobles en memoria (CA1-CA6)."""

import json
import unittest
from datetime import date, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from fakes import (
    NOW,
    FakePermissions,
    FixedClock,
    InMemoryProjectionUnitOfWork,
    InMemoryReadStore,
)
from nutrition_fakes import (
    AUTHOR,
    NO_HABITS,
    FakeRestrictions,
    InMemoryNutritionPlanReader,
    InMemoryNutritionStore,
    InMemoryNutritionUnitOfWork,
    StaticFoodCatalog,
    habits,
)
from test_workers import Message

from training_management.application.commands.create_nutrition_plan import (
    CreateNutritionPlan,
    CreateNutritionPlanCommand,
    ItemInput,
    MealInput,
)
from training_management.application.errors import (
    AthleteAuthorizationRequired,
    IdempotencyKeyReused,
    NutritionPlanNotFound,
    NutritionRestrictionsUnavailable,
    PermissionRequired,
)
from training_management.application.messages import (
    InvalidEvent,
    NutritionPlanCreatedEvent,
    parse_training_event,
)
from training_management.application.ports import AuthorizationView, Caller, IdempotencyRecord
from training_management.application.projections.plan_projection import ProjectionOutcome
from training_management.application.projections.snapshot_projection import TrainingProjector
from training_management.application.queries.food_catalog import GetFoodCatalog
from training_management.application.queries.nutrition_plans import (
    GetNutritionPlan,
    InvalidNutritionPlanQuery,
    ListMyNutritionPlans,
    NutritionPlanFilters,
)
from training_management.domain.authorizations.model import TrainingAuthorization
from training_management.domain.nutrition.catalog import FOOD_CATALOG
from training_management.domain.nutrition.model import InvalidNutritionPlan, MealType
from training_management.infrastructure.persistence_write.outbox import ClaimedEvent
from training_management.workers.outbox_publisher import build_envelope
from training_management.workers.read_projector import ProjectionMessageHandler


def command(athlete_id: UUID, **overrides: Any) -> CreateNutritionPlanCommand:
    values: dict[str, Any] = {
        "athlete_id": athlete_id,
        "title": "Plan de recuperación",
        "objectives": ("Hidratación diaria",),
        "start_date": date(2026, 11, 2),
        "end_date": date(2026, 11, 29),
        "meals": (
            MealInput(
                MealType.BREAKFAST,
                (ItemInput("OATS", Decimal("80"), "G"), ItemInput("BANANA", Decimal("1"), "UNIT")),
            ),
            MealInput(MealType.LUNCH, (ItemInput("CHICKEN_BREAST", Decimal("150.0"), "G"),)),
        ),
        "idempotency_key": uuid4(),
        "correlation_id": uuid4(),
    }
    values.update(overrides)
    return CreateNutritionPlanCommand(**values)


def lunch(food_code: str, quantity: str = "100", unit: str = "G") -> tuple[MealInput, ...]:
    return (MealInput(MealType.LUNCH, (ItemInput(food_code, Decimal(quantity), unit),)),)


class NutritionTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.store = InMemoryNutritionStore()
        self.clock = FixedClock()
        self.athlete = Caller(uuid4(), "athlete")
        self.professional = Caller(uuid4(), "service_provider")
        self.permissions = FakePermissions({"service_provider": {AUTHOR}})
        self.restrictions = FakeRestrictions(habits())
        self.catalog = StaticFoodCatalog()
        self.on_check: Any = None
        self.create = self.use_case()

    def use_case(self, before_commit: Any = None) -> CreateNutritionPlan:
        return CreateNutritionPlan(
            unit_of_work=lambda: InMemoryNutritionUnitOfWork(
                self.store, before_commit, self.on_check
            ),
            permissions=self.permissions,
            catalog=self.catalog,
            restrictions=self.restrictions,
            clock=self.clock,
        )

    def authorize(self, athlete: Caller | None = None) -> None:
        athlete_id = (athlete or self.athlete).user_id
        self.store.authorizations[(athlete_id, self.professional.user_id)] = (
            TrainingAuthorization.grant(
                athlete_id=athlete_id, professional_id=self.professional.user_id, now=NOW
            )
        )

    def revoke(self) -> None:
        key = (self.athlete.user_id, self.professional.user_id)
        self.store.authorizations[key] = self.store.authorizations[key].revoke(NOW)


class CreateNutritionPlanTest(NutritionTestCase):
    def test_authorized_professional_creates_assigned_plan_and_event(self) -> None:
        """CA1, CA4 y CA5: plan `ASSIGNED` v1, evento en la outbox y clave registrada."""
        self.authorize()
        cmd = command(self.athlete.user_id)

        result = self.create(self.professional, cmd)

        plan = self.store.plans[result.plan.plan_id]
        self.assertEqual(
            (plan.athlete_id, plan.professional_id), (cmd.athlete_id, self.professional.user_id)
        )
        self.assertEqual((plan.status.value, plan.version), ("ASSIGNED", 1))
        self.assertEqual(plan.meals[1].items[0].quantity, Decimal("150.0"))
        self.assertEqual(result.warnings, ())
        self.assertFalse(result.replayed)
        [event] = self.store.outbox
        self.assertEqual(
            (event.event_type, event.aggregate_type, event.aggregate_id, event.aggregate_version),
            ("plan.alimentacion_creado.v1", "nutrition_plan", plan.plan_id, 1),
        )
        self.assertEqual(event.causation_id, str(cmd.idempotency_key))
        self.assertEqual(event.correlation_id, cmd.correlation_id)
        self.assertEqual(event.payload["meals"][0]["items"][0]["quantity"], "80.0")
        self.assertEqual(
            self.store.idempotency[cmd.idempotency_key].operation, "create_nutrition_plan"
        )
        self.assertEqual(self.restrictions.calls, [self.athlete.user_id])

    def test_missing_habits_create_the_plan_with_a_warning(self) -> None:
        """CA6: sin hábitos registrados el plan se crea con `nutrition_habits_missing`."""
        self.authorize()
        self.restrictions.result = NO_HABITS
        cmd = command(self.athlete.user_id)

        first = self.create(self.professional, cmd)
        again = self.create(self.professional, cmd)

        self.assertEqual(first.warnings, ("nutrition_habits_missing",))
        self.assertEqual((again.replayed, again.warnings), (True, ("nutrition_habits_missing",)))
        self.assertEqual(again.plan, first.plan)
        self.assertNotIn("warnings", self.store.outbox[0].payload)
        self.assertEqual(len(self.restrictions.calls), 1, "el reintento no consulta Usuarios")

    def test_retry_with_same_key_returns_original_result(self) -> None:
        """CA1: reintentos con la misma clave → un plan y un evento."""
        self.authorize()
        cmd = command(self.athlete.user_id)

        results = [self.create(self.professional, cmd) for _ in range(8)]

        self.assertEqual({r.plan.plan_id for r in results}, {results[0].plan.plan_id})
        self.assertEqual([r.replayed for r in results], [False] + [True] * 7)
        self.assertEqual((len(self.store.plans), len(self.store.outbox)), (1, 1))

    def test_equivalent_quantity_is_the_same_request(self) -> None:
        self.authorize()
        cmd = command(self.athlete.user_id, meals=lunch("OATS", "80"))
        self.create(self.professional, cmd)

        again = self.create(
            self.professional,
            command(
                self.athlete.user_id,
                meals=lunch("OATS", "80.0"),
                idempotency_key=cmd.idempotency_key,
            ),
        )

        self.assertTrue(again.replayed)

    def test_key_reused_with_other_request_or_professional_is_rejected(self) -> None:
        self.authorize()
        cmd = command(self.athlete.user_id)
        self.create(self.professional, cmd)
        other_professional = Caller(uuid4(), "service_provider")

        with self.assertRaises(IdempotencyKeyReused):
            self.create(
                self.professional,
                command(
                    self.athlete.user_id, title="Otro plan", idempotency_key=cmd.idempotency_key
                ),
            )
        with self.assertRaises(IdempotencyKeyReused):
            self.create(other_professional, cmd)
        self.assertEqual(len(self.store.plans), 1)

    def test_expired_key_creates_a_new_plan(self) -> None:
        self.authorize()
        cmd = command(self.athlete.user_id)
        first = self.create(self.professional, cmd)
        self.clock.advance(timedelta(hours=24, seconds=1))

        second = self.create(self.professional, cmd)

        self.assertNotEqual(second.plan.plan_id, first.plan.plan_id)
        self.assertFalse(second.replayed)

    def test_key_of_another_operation_is_rejected(self) -> None:
        self.authorize()
        cmd = command(self.athlete.user_id)
        self.store.idempotency[cmd.idempotency_key] = IdempotencyRecord(
            key=cmd.idempotency_key,
            operation="create_training_plan",
            request_fingerprint="x" * 64,
            resource_id=uuid4(),
            response={},
            created_at=NOW,
            expires_at=NOW + timedelta(hours=1),
        )

        with self.assertRaises(IdempotencyKeyReused):
            self.create(self.professional, cmd)

    def test_concurrent_winner_is_returned(self) -> None:
        """La misma clave confirmada por otra solicitud entre la consulta y el COMMIT."""
        self.authorize()
        cmd = command(self.athlete.user_id)
        winner = self.use_case()
        winners: list[Any] = []

        create = self.use_case(before_commit=lambda: winners.append(winner(self.professional, cmd)))
        result = create(self.professional, cmd)

        self.assertTrue(result.replayed)
        self.assertEqual(result.plan.plan_id, winners[0].plan.plan_id)
        self.assertEqual((len(self.store.plans), len(self.store.outbox)), (1, 1))

    def test_requires_author_permission(self) -> None:
        """CA3: otro tipo de actor o sin `nutrition.plan.author` → 403, sin efectos."""
        self.authorize()
        for caller in (self.athlete, Caller(uuid4(), "organizer")):
            with self.subTest(actor=caller.actor_type), self.assertRaises(PermissionRequired):
                self.create(caller, command(self.athlete.user_id))
        self.permissions = FakePermissions({"service_provider": {"training.plan.author"}})
        with self.assertRaises(PermissionRequired):
            self.use_case()(self.professional, command(self.athlete.user_id))
        self.assertEqual((self.catalog.loads, self.restrictions.calls), (0, []))
        self.assertEqual(self.store.plans, {})

    def test_requires_active_authorization_without_reading_restrictions(self) -> None:
        """CA3: sin autorización (o revocada) → 403 y Usuarios nunca se consulta."""
        with self.assertRaises(AthleteAuthorizationRequired):
            self.create(self.professional, command(self.athlete.user_id))
        self.authorize()
        self.revoke()
        with self.assertRaises(AthleteAuthorizationRequired):
            self.create(self.professional, command(self.athlete.user_id))

        self.assertEqual(self.restrictions.calls, [])
        self.assertEqual(
            (self.store.plans, self.store.outbox, self.store.idempotency), ({}, [], {})
        )

    def test_revocation_after_restrictions_rejects_inside_the_transaction(self) -> None:
        self.authorize()
        self.on_check = lambda checks: self.revoke() if checks == 2 else None
        create = self.use_case()

        with self.assertRaises(AthleteAuthorizationRequired):
            create(self.professional, command(self.athlete.user_id))

        self.assertEqual(len(self.restrictions.calls), 1)
        self.assertEqual((self.store.plans, self.store.outbox), ({}, []))

    def test_rules_fail_before_reading_restrictions(self) -> None:
        self.authorize()

        with self.assertRaises(InvalidNutritionPlan) as error:
            self.create(self.professional, command(self.athlete.user_id, meals=lunch("PIZZA")))

        self.assertEqual(
            [(v.field, v.issue) for v in error.exception.violations],
            [("meals.0.items.0.food_code", "unknown_value")],
        )
        self.assertEqual(self.restrictions.calls, [])

    def test_conflicts_block_the_plan(self) -> None:
        """CA2: `VEGAN` con `CHICKEN_BREAST`, `GLUTEN_FREE` con `PASTA` y alergia a `PEANUTS`."""
        self.authorize()
        cases = (
            (habits(diet="VEGAN"), "CHICKEN_BREAST", "conflicts_with_restriction"),
            (habits(restrictions=("GLUTEN_FREE",)), "PASTA", "conflicts_with_restriction"),
            (habits(allergies=("PEANUTS",)), "PEANUT_BUTTER", "conflicts_with_allergy"),
        )
        for restrictions, food_code, issue in cases:
            self.restrictions.result = restrictions
            with self.subTest(food=food_code), self.assertRaises(InvalidNutritionPlan) as error:
                self.create(
                    self.professional, command(self.athlete.user_id, meals=lunch(food_code))
                )
            self.assertTrue(error.exception.blocked)
            self.assertEqual(
                [(v.field, v.issue) for v in error.exception.violations],
                [("meals.0.items.0.food_code", issue)],
            )
        self.assertEqual(
            (self.store.plans, self.store.outbox, self.store.idempotency), ({}, [], {})
        )

    def test_users_unavailable_creates_nothing(self) -> None:
        """CA6: Usuarios caído → `upstream_unavailable` sin plan."""
        self.authorize()
        self.restrictions.unavailable = True

        with self.assertRaises(NutritionRestrictionsUnavailable):
            self.create(self.professional, command(self.athlete.user_id))

        self.assertEqual(
            (self.store.plans, self.store.outbox, self.store.idempotency), ({}, [], {})
        )

    def test_audit_has_identifiers_but_no_foods_or_restrictions(self) -> None:
        """CA5: auditoría de creación y de rechazos sin alimentos ni restricciones."""
        self.authorize()
        self.restrictions.result = habits(diet="VEGAN", allergies=("PEANUTS",))
        with self.assertLogs("sportapp.audit", level="INFO") as logs:
            with self.assertRaises(InvalidNutritionPlan):
                self.create(self.professional, command(self.athlete.user_id))
            self.restrictions.result = habits()
            created = self.create(self.professional, command(self.athlete.user_id))

        fields = [record.__dict__["fields"] for record in logs.records]
        self.assertEqual(
            [record.getMessage() for record in logs.records], ["audit.nutrition_plan_created"] * 2
        )
        self.assertEqual(
            (fields[0]["result"], fields[0]["reason"], fields[0]["blocked"]),
            ("rejected", "nutrition_plan_invalid", True),
        )
        self.assertEqual(
            (fields[1]["result"], fields[1]["blocked"], fields[1]["plan_id"]),
            ("created", False, str(created.plan.plan_id)),
        )
        serialized = json.dumps(fields)
        for sensitive in ("CHICKEN_BREAST", "OATS", "VEGAN", "PEANUTS", "meals", "allerg"):
            self.assertNotIn(sensitive, serialized)


class NutritionQueriesTest(NutritionTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.read = InMemoryReadStore()
        self.authorize()
        self.plan = self.create(self.professional, command(self.athlete.user_id)).plan
        self.read.nutrition_plans[self.plan.plan_id] = self.plan
        self.read.authorizations[(self.athlete.user_id, self.professional.user_id)] = (
            AuthorizationView(
                self.athlete.user_id, self.professional.user_id, "ACTIVE", 1, NOW, None
            )
        )
        reader = InMemoryNutritionPlanReader(self.read.nutrition_plans)
        self.get = GetNutritionPlan(
            reader=reader,
            authorizations=_Authorizations(self.read),
            permissions=self.permissions,
        )
        self.mine = ListMyNutritionPlans(reader=reader)

    def test_creator_and_owner_see_the_plan(self) -> None:
        self.assertEqual(self.get(self.professional, self.plan.plan_id), self.plan)
        self.assertEqual(self.get(self.athlete, self.plan.plan_id), self.plan)

    def test_third_parties_and_missing_plans_are_not_found(self) -> None:
        """CA3: un tercero no distingue un plan ajeno de uno inexistente."""
        for caller, plan_id in (
            (Caller(uuid4(), "athlete"), self.plan.plan_id),
            (Caller(uuid4(), "service_provider"), self.plan.plan_id),
            (self.athlete, uuid4()),
            (self.professional, uuid4()),
        ):
            with self.subTest(caller=caller.actor_type), self.assertRaises(NutritionPlanNotFound):
                self.get(caller, plan_id)

    def test_other_actor_or_missing_permission_is_forbidden(self) -> None:
        with self.assertRaises(PermissionRequired):
            self.get(Caller(uuid4(), "organizer"), self.plan.plan_id)
        no_permission = GetNutritionPlan(
            reader=InMemoryNutritionPlanReader(self.read.nutrition_plans),
            authorizations=_Authorizations(self.read),
            permissions=FakePermissions({"service_provider": set()}),
        )
        with self.assertRaises(PermissionRequired):
            no_permission(self.professional, self.plan.plan_id)

    def test_creator_with_revoked_authorization_is_forbidden(self) -> None:
        self.read.authorizations[(self.athlete.user_id, self.professional.user_id)] = (
            AuthorizationView(
                self.athlete.user_id, self.professional.user_id, "REVOKED", 2, NOW, NOW
            )
        )

        with self.assertRaises(AthleteAuthorizationRequired):
            self.get(self.professional, self.plan.plan_id)
        self.read.authorizations.clear()
        with self.assertRaises(AthleteAuthorizationRequired):
            self.get(self.professional, self.plan.plan_id)
        self.assertEqual(self.get(self.athlete, self.plan.plan_id), self.plan)

    def test_my_plans_are_paginated_without_gaps_or_repeats(self) -> None:
        for _ in range(4):
            self.clock.advance(timedelta(minutes=1))
            plan = self.create(self.professional, command(self.athlete.user_id)).plan
            self.read.nutrition_plans[plan.plan_id] = plan
        expected = sorted(
            self.read.nutrition_plans.values(),
            key=lambda p: (p.created_at, p.plan_id),
            reverse=True,
        )

        seen: list[Any] = []
        cursor = None
        while True:
            page = self.mine(self.athlete, NutritionPlanFilters(limit=2, cursor=cursor))
            seen.extend(page.items)
            if page.next_cursor is None:
                break
            cursor = page.next_cursor

        self.assertEqual(seen, expected)
        self.assertEqual(len(self.mine(self.athlete, NutritionPlanFilters()).items), 5)
        self.assertEqual(self.mine(Caller(uuid4(), "athlete"), NutritionPlanFilters()).items, ())

    def test_my_plans_validation(self) -> None:
        for filters, field, issue in (
            (NutritionPlanFilters(limit=0), "limit", "out_of_range"),
            (NutritionPlanFilters(limit=51), "limit", "out_of_range"),
            (NutritionPlanFilters(cursor="no-es-un-cursor"), "cursor", "invalid_cursor"),
            (
                NutritionPlanFilters(cursor="eyJjIjoiMjAyNi0xMC0yMlQxMjowMDowMCIsInAiOiIxIn0"),
                "cursor",
                "invalid_cursor",
            ),
        ):
            with self.subTest(field=field), self.assertRaises(InvalidNutritionPlanQuery) as error:
                self.mine(self.athlete, filters)
            self.assertEqual((error.exception.field, error.exception.issue), (field, issue))
        self.assertEqual(len(self.mine(self.athlete, NutritionPlanFilters(limit=50)).items), 1)
        with self.assertRaises(PermissionRequired):
            self.mine(self.professional, NutritionPlanFilters())

    def test_food_catalog_for_any_actor(self) -> None:
        get_catalog = GetFoodCatalog(catalog=self.catalog)

        self.assertIs(get_catalog(Caller(uuid4(), "organizer")), FOOD_CATALOG)


class _Authorizations:
    def __init__(self, store: InMemoryReadStore) -> None:
        self.store = store

    def get(self, athlete_id: UUID, professional_id: UUID) -> AuthorizationView | None:
        return self.store.authorizations.get((athlete_id, professional_id))


class NutritionProjectionTest(NutritionTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.authorize()
        self.created = self.create(self.professional, command(self.athlete.user_id)).plan
        self.read = InMemoryReadStore()
        self.project = TrainingProjector(
            unit_of_work=lambda: InMemoryProjectionUnitOfWork(self.read), clock=self.clock
        )

    def envelope(self) -> dict[str, Any]:
        message = self.store.outbox[0]
        return build_envelope(
            ClaimedEvent(
                event_id=message.event_id,
                event_type=message.event_type,
                schema_version=message.schema_version,
                aggregate_type=message.aggregate_type,
                aggregate_id=message.aggregate_id,
                aggregate_version=message.aggregate_version,
                occurred_at=message.occurred_at,
                correlation_id=message.correlation_id,
                causation_id=message.causation_id,
                payload=message.payload,
                attempts=0,
            )
        )

    def test_projects_once_and_ignores_duplicates(self) -> None:
        """CA5: el evento publicado se proyecta una vez; la vista coincide con el plan."""
        event = parse_training_event(self.envelope())
        assert isinstance(event, NutritionPlanCreatedEvent)

        self.assertEqual(self.project(event), ProjectionOutcome.PROJECTED)
        self.assertEqual(self.project(event), ProjectionOutcome.DUPLICATE)
        self.assertEqual(self.read.nutrition_plans[self.created.plan_id], self.created)
        self.assertEqual(len(self.read.processed), 1)

    def test_rebuild_and_versions(self) -> None:
        """Reconstrucción: otro `event_id` con la misma versión queda superado; una versión
        mayor reemplaza el estado completo."""
        envelope = self.envelope()
        self.project(parse_training_event(envelope))

        rebuilt = parse_training_event({**envelope, "event_id": str(uuid4())})
        newer_payload = {**envelope["payload"], "version": 2, "title": "Plan ajustado"}
        newer = parse_training_event(
            {**envelope, "event_id": str(uuid4()), "aggregate_version": 2, "payload": newer_payload}
        )

        self.assertEqual(self.project(rebuilt), ProjectionOutcome.STALE)
        self.assertEqual(self.project(newer), ProjectionOutcome.PROJECTED)
        self.assertEqual(self.read.nutrition_plans[self.created.plan_id].title, "Plan ajustado")
        self.assertEqual(self.project(rebuilt), ProjectionOutcome.DUPLICATE)
        self.assertEqual(len(self.read.processed), 3)

    def test_worker_acks_projected_messages_and_nacks_invalid_ones(self) -> None:
        """El proyector confirma después del COMMIT; un evento inválido no se confirma (DLQ)."""
        handler = ProjectionMessageHandler(self.project)
        valid = Message(json.loads(json.dumps(self.envelope())))
        invalid = Message({**self.envelope(), "aggregate_id": str(uuid4())})

        with self.assertLogs("sportapp.projector", level="INFO") as logs:
            handler(valid)
            handler(invalid)

        self.assertTrue(valid.acked)
        self.assertTrue(invalid.nacked and not invalid.acked)
        self.assertIn(self.created.plan_id, self.read.nutrition_plans)
        fields = logs.records[0].__dict__["fields"]
        self.assertEqual(fields["aggregate_id"], str(self.created.plan_id))

    def test_invalid_events_are_rejected(self) -> None:
        envelope = self.envelope()
        payload = envelope["payload"]
        item = payload["meals"][0]["items"][0]
        meal = payload["meals"][0]
        broken: list[dict[str, Any]] = [
            {**envelope, "aggregate_id": str(uuid4())},
            {**envelope, "schema_version": 2},
            {**envelope, "aggregate_version": 3},
            {**envelope, "payload": {**payload, "status": "DRAFT"}},
            {**envelope, "payload": {**payload, "title": ""}},
            {**envelope, "payload": {**payload, "objectives": []}},
            {**envelope, "payload": {**payload, "objectives": [1]}},
            {**envelope, "payload": {**payload, "end_date": "2026-11-01"}},
            {**envelope, "payload": {**payload, "meals": []}},
            {**envelope, "payload": {**payload, "meals": ["x"]}},
            {**envelope, "payload": {**payload, "meals": [{**meal, "meal_type": "BRUNCH"}]}},
            {**envelope, "payload": {**payload, "meals": [{**meal, "items": ["x"]}]}},
            {
                **envelope,
                "payload": {
                    **payload,
                    "meals": [{**meal, "items": [{**item, "food_code": "oats"}]}],
                },
            },
            {
                **envelope,
                "payload": {**payload, "meals": [{**meal, "items": [{**item, "quantity": "80"}]}]},
            },
            {
                **envelope,
                "payload": {**payload, "meals": [{**meal, "items": [{**item, "quantity": "0.0"}]}]},
            },
            {
                **envelope,
                "payload": {**payload, "meals": [{**meal, "items": [{**item, "unit": "KG"}]}]},
            },
            {**envelope, "payload": {**payload, "catalog_version": ""}},
            {**envelope, "payload": {**payload, "created_at": "2026-10-22T12:00:00"}},
        ]
        for index, candidate in enumerate(broken):
            with self.subTest(case=index), self.assertRaises(InvalidEvent):
                parse_training_event(candidate)


if __name__ == "__main__":
    unittest.main()
