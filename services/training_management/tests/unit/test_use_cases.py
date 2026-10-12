"""Casos de uso de HU010 con dobles en memoria (CA1-CA5)."""

import unittest
from datetime import date, timedelta
from typing import Any
from uuid import UUID, uuid4

from fakes import (
    NOW,
    FakeCapabilities,
    FakePermissions,
    FixedClock,
    InMemoryAuthorizationUnitOfWork,
    InMemoryPlanningUnitOfWork,
    InMemoryPlanReader,
    InMemoryProjectionUnitOfWork,
    InMemoryReadStore,
    InMemoryTrainingStore,
)

from training_management.application.commands.authorizations import (
    GrantAuthorization,
    RevokeAuthorization,
)
from training_management.application.commands.create_training_plan import (
    CreateTrainingPlan,
    CreateTrainingPlanCommand,
    SessionInput,
)
from training_management.application.errors import (
    AthleteAuthorizationRequired,
    CapabilitiesUnavailable,
    CapabilityRequired,
    IdempotencyKeyReused,
    PermissionRequired,
    TrainingPlanNotFound,
)
from training_management.application.messages import PlanCreatedEvent
from training_management.application.ports import Caller, PlanView
from training_management.application.projections.plan_projection import (
    ProjectionGap,
    ProjectionOutcome,
    ProjectPlanCreated,
)
from training_management.application.queries.get_training_plan import GetTrainingPlan
from training_management.domain.authorizations.model import TrainingAuthorization
from training_management.domain.plans.errors import InvalidTrainingPlan
from training_management.domain.plans.model import Sport

MANAGE = "training.plan.manage"


def command(athlete_id: UUID, **overrides: Any) -> CreateTrainingPlanCommand:
    values: dict[str, Any] = {
        "athlete_id": athlete_id,
        "title": "Base aeróbica",
        "objectives": ("Correr 10 km",),
        "start_date": date(2026, 11, 2),
        "end_date": date(2026, 11, 29),
        "sessions": (
            SessionInput(date(2026, 11, 2), Sport.RUNNING, 40, 5),
            SessionInput(date(2026, 11, 4), Sport.STRENGTH, 30, 6),
        ),
        "idempotency_key": uuid4(),
        "correlation_id": uuid4(),
    }
    values.update(overrides)
    return CreateTrainingPlanCommand(**values)


class PlanningTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.store = InMemoryTrainingStore()
        self.clock = FixedClock()
        self.athlete = Caller(uuid4(), "athlete")
        self.professional = Caller(uuid4(), "service_provider")
        self.capabilities = FakeCapabilities({self.athlete.user_id: {MANAGE}})
        self.create = CreateTrainingPlan(
            unit_of_work=lambda: InMemoryPlanningUnitOfWork(self.store),
            permissions=FakePermissions(),
            clock=self.clock,
        )
        self.grant = GrantAuthorization(
            unit_of_work=lambda: InMemoryAuthorizationUnitOfWork(self.store),
            capabilities=self.capabilities,
            clock=self.clock,
        )
        self.revoke = RevokeAuthorization(
            unit_of_work=lambda: InMemoryAuthorizationUnitOfWork(self.store), clock=self.clock
        )

    def authorize(self) -> None:
        self.grant(self.athlete, self.professional.user_id)


class CreateTrainingPlanTest(PlanningTestCase):
    def test_authorized_professional_creates_plan_and_event(self) -> None:
        """CA1 y CA4: plan asociado a deportista, creador y periodo; evento en la outbox."""
        self.authorize()
        cmd = command(self.athlete.user_id)

        result = self.create(self.professional, cmd)

        plan = self.store.plans[result.plan.plan_id]
        self.assertEqual(plan.athlete_id, self.athlete.user_id)
        self.assertEqual(plan.professional_id, self.professional.user_id)
        self.assertEqual((plan.start_date, plan.end_date), (cmd.start_date, cmd.end_date))
        self.assertFalse(result.replayed)
        [event] = self.store.outbox
        self.assertEqual(event.event_type, "plan.entrenamiento_creado.v1")
        self.assertEqual((event.aggregate_id, event.aggregate_version), (plan.plan_id, 1))
        self.assertEqual(event.causation_id, str(cmd.idempotency_key))
        self.assertEqual(event.correlation_id, cmd.correlation_id)

    def test_without_athlete_authorization_is_rejected(self) -> None:
        """CA2: relación inexistente."""
        with self.assertRaises(AthleteAuthorizationRequired):
            self.create(self.professional, command(self.athlete.user_id))

        self.assertEqual((self.store.plans, self.store.outbox), ({}, []))

    def test_revoked_authorization_is_rejected(self) -> None:
        """CA2: relación revocada."""
        self.authorize()
        self.revoke(self.athlete, self.professional.user_id)

        with self.assertRaises(AthleteAuthorizationRequired):
            self.create(self.professional, command(self.athlete.user_id))

    def test_authorization_for_another_professional_does_not_count(self) -> None:
        self.authorize()
        other = Caller(uuid4(), "service_provider")

        with self.assertRaises(AthleteAuthorizationRequired):
            self.create(other, command(self.athlete.user_id))

    def test_actor_without_author_permission_is_rejected(self) -> None:
        """CA2: rol sin permiso (deportista u organizador)."""
        for actor in ("athlete", "organizer"):
            with self.subTest(actor=actor), self.assertRaises(PermissionRequired):
                self.create(Caller(uuid4(), actor), command(self.athlete.user_id))

    def test_incoherent_plan_is_not_saved(self) -> None:
        """CA3: no se guarda y se informan las reglas incumplidas."""
        self.authorize()

        with self.assertRaises(InvalidTrainingPlan) as raised:
            self.create(
                self.professional,
                command(
                    self.athlete.user_id,
                    start_date=date(2026, 11, 29),
                    end_date=date(2026, 11, 2),
                ),
            )

        self.assertTrue(raised.exception.violations)
        self.assertEqual((self.store.plans, self.store.outbox), ({}, []))

    def test_idempotent_retry_returns_same_plan(self) -> None:
        """CA5: el reintento no duplica el plan."""
        self.authorize()
        cmd = command(self.athlete.user_id)

        first = self.create(self.professional, cmd)
        second = self.create(self.professional, cmd)

        self.assertTrue(second.replayed)
        self.assertEqual(second.plan, first.plan)
        self.assertEqual((len(self.store.plans), len(self.store.outbox)), (1, 1))

    def test_same_key_with_other_payload_is_rejected(self) -> None:
        self.authorize()
        cmd = command(self.athlete.user_id)
        self.create(self.professional, cmd)

        with self.assertRaises(IdempotencyKeyReused):
            self.create(
                self.professional,
                command(self.athlete.user_id, idempotency_key=cmd.idempotency_key, title="Otro"),
            )

    def test_same_key_from_another_professional_never_returns_foreign_plan(self) -> None:
        self.authorize()
        cmd = command(self.athlete.user_id)
        self.create(self.professional, cmd)
        other = Caller(uuid4(), "service_provider")
        self.grant(self.athlete, other.user_id)

        with self.assertRaises(IdempotencyKeyReused):
            self.create(other, cmd)

    def test_expired_key_creates_a_new_plan(self) -> None:
        self.authorize()
        cmd = command(self.athlete.user_id)
        self.create(self.professional, cmd)
        self.clock.advance(timedelta(hours=25))

        result = self.create(self.professional, cmd)

        self.assertFalse(result.replayed)
        self.assertEqual(len(self.store.plans), 2)

    def test_concurrent_retry_returns_the_winner(self) -> None:
        """CA5 con concurrencia: otra solicitud con la misma clave confirma primero."""
        self.authorize()
        cmd = command(self.athlete.user_id)
        winner = CreateTrainingPlan(
            unit_of_work=lambda: InMemoryPlanningUnitOfWork(self.store),
            permissions=FakePermissions(),
            clock=self.clock,
        )
        # Lectura previa sin resultado, transacción que pierde la carrera y relectura.
        units = iter(
            [
                InMemoryPlanningUnitOfWork(self.store),
                InMemoryPlanningUnitOfWork(self.store, lambda: winner(self.professional, cmd)),
                InMemoryPlanningUnitOfWork(self.store),
            ]
        )
        loser = CreateTrainingPlan(
            unit_of_work=units.__next__, permissions=FakePermissions(), clock=self.clock
        )

        result = loser(self.professional, cmd)

        self.assertTrue(result.replayed)
        self.assertEqual(len(self.store.plans), 1)

    def test_rejections_are_audited_without_plan_content(self) -> None:
        with self.assertLogs("sportapp.audit", level="INFO") as logs:
            with self.assertRaises(AthleteAuthorizationRequired):
                self.create(self.professional, command(self.athlete.user_id))

        fields = logs.records[0].fields  # type: ignore[attr-defined]
        self.assertEqual(fields["reason"], "athlete_authorization_required")
        self.assertNotIn("Base aeróbica", str(fields))


class AuthorizationUseCasesTest(PlanningTestCase):
    def test_grant_requires_capability(self) -> None:
        self.capabilities.granted = {}

        with self.assertRaises(CapabilityRequired):
            self.grant(self.athlete, self.professional.user_id)

        self.assertEqual(self.store.authorizations, {})

    def test_grant_never_assumes_capability_when_affiliation_fails(self) -> None:
        self.capabilities.unavailable = True

        with self.assertRaises(CapabilitiesUnavailable):
            self.grant(self.athlete, self.professional.user_id)

    def test_only_athletes_grant_or_revoke(self) -> None:
        for operation in (self.grant, self.revoke):
            with self.subTest(operation=type(operation).__name__):
                with self.assertRaises(PermissionRequired):
                    operation(self.professional, uuid4())

    def test_grant_is_idempotent_and_reactivates_after_revocation(self) -> None:
        first = self.grant(self.athlete, self.professional.user_id)
        again = self.grant(self.athlete, self.professional.user_id)
        self.revoke(self.athlete, self.professional.user_id)
        reactivated = self.grant(self.athlete, self.professional.user_id)

        self.assertEqual((first.created, again.created, reactivated.created), (True, False, True))
        self.assertEqual(reactivated.authorization.version, 3)

    def test_revoke_without_capability_and_twice_is_harmless(self) -> None:
        self.authorize()
        self.capabilities.granted = {}

        self.revoke(self.athlete, self.professional.user_id)
        self.revoke(self.athlete, self.professional.user_id)
        self.revoke(self.athlete, uuid4())

        key = (self.athlete.user_id, self.professional.user_id)
        self.assertFalse(self.store.authorizations[key].is_active)

    def test_concurrent_first_grant_returns_existing(self) -> None:
        competitor = TrainingAuthorization.grant(
            athlete_id=self.athlete.user_id, professional_id=self.professional.user_id, now=NOW
        )
        store = self.store

        def insert_first() -> None:
            store.authorizations[(competitor.athlete_id, competitor.professional_id)] = competitor

        grant = GrantAuthorization(
            unit_of_work=iter(
                [
                    InMemoryAuthorizationUnitOfWork(store, insert_first),
                    InMemoryAuthorizationUnitOfWork(store),
                ]
            ).__next__,
            capabilities=self.capabilities,
            clock=self.clock,
        )

        result = grant(self.athlete, self.professional.user_id)

        self.assertFalse(result.created)
        self.assertEqual(result.authorization, competitor)


class GetTrainingPlanTest(PlanningTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.read = InMemoryReadStore()
        self.get = GetTrainingPlan(
            reader=InMemoryPlanReader(self.read),
            permissions=FakePermissions(),
            capabilities=self.capabilities,
        )
        self.authorize()
        created = self.create(self.professional, command(self.athlete.user_id))
        self.view = created.plan
        self.read.views[self.view.plan_id] = self.view

    def test_creator_and_assigned_athlete_can_read(self) -> None:
        """CA4."""
        self.assertEqual(self.get(self.professional, self.view.plan_id), self.view)
        self.assertEqual(self.get(self.athlete, self.view.plan_id), self.view)

    def test_other_users_get_not_found(self) -> None:
        for caller in (Caller(uuid4(), "service_provider"), Caller(uuid4(), "athlete")):
            with self.subTest(actor=caller.actor_type), self.assertRaises(TrainingPlanNotFound):
                self.get(caller, self.view.plan_id)

    def test_not_projected_yet_is_not_found(self) -> None:
        with self.assertRaises(TrainingPlanNotFound):
            self.get(self.professional, uuid4())

    def test_athlete_without_capability_is_forbidden(self) -> None:
        self.capabilities.granted = {}

        with self.assertRaises(CapabilityRequired):
            self.get(self.athlete, self.view.plan_id)


class ProjectPlanCreatedTest(unittest.TestCase):
    def setUp(self) -> None:
        self.read = InMemoryReadStore()
        self.project = ProjectPlanCreated(
            unit_of_work=lambda: InMemoryProjectionUnitOfWork(self.read), clock=FixedClock()
        )

    def event(self, version: int = 1, plan_id: UUID | None = None) -> PlanCreatedEvent:
        view = PlanView(
            plan_id=plan_id or uuid4(),
            athlete_id=uuid4(),
            professional_id=uuid4(),
            title="Base",
            objectives=("Correr",),
            start_date=date(2026, 11, 2),
            end_date=date(2026, 11, 29),
            status="ASSIGNED",
            version=version,
            sessions=(),
            created_at=NOW,
        )
        return PlanCreatedEvent(
            event_id=uuid4(), aggregate_version=version, correlation_id=None, plan=view
        )

    def test_projects_once_and_ignores_duplicates(self) -> None:
        event = self.event()

        outcomes = [self.project(event), self.project(event)]

        self.assertEqual(outcomes, [ProjectionOutcome.PROJECTED, ProjectionOutcome.DUPLICATE])
        self.assertEqual(self.read.views[event.plan.plan_id], event.plan)
        self.assertEqual(len(self.read.processed), 1)

    def test_concurrent_duplicate_delivery(self) -> None:
        event = self.event()
        project = ProjectPlanCreated(
            unit_of_work=lambda: InMemoryProjectionUnitOfWork(
                self.read, lambda: self.project(event)
            ),
            clock=FixedClock(),
        )

        self.assertEqual(project(event), ProjectionOutcome.DUPLICATE)
        self.assertEqual(len(self.read.processed), 1)

    def test_older_or_equal_version_is_stale(self) -> None:
        first = self.event()
        self.project(first)
        replay = self.event(version=1, plan_id=first.plan.plan_id)

        self.assertEqual(self.project(replay), ProjectionOutcome.STALE)
        self.assertEqual(len(self.read.processed), 2)

    def test_version_gap_is_retried_without_effects(self) -> None:
        with self.assertLogs("sportapp.projector", level="WARNING"):
            with self.assertRaises(ProjectionGap):
                self.project(self.event(version=2))

        self.assertEqual((self.read.views, self.read.processed), ({}, {}))


if __name__ == "__main__":
    unittest.main()
