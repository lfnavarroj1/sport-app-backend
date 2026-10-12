"""Reglas de coherencia del plan (HU010 CA1 y CA3), sin infraestructura."""

import unittest
from datetime import date, timedelta
from typing import Any
from uuid import uuid4

from fakes import NOW

from training_management.domain.authorizations.model import (
    AuthorizationStatus,
    SelfAuthorization,
    TrainingAuthorization,
)
from training_management.domain.plans.errors import InvalidTrainingPlan, Violation
from training_management.domain.plans.events import plan_created_payload
from training_management.domain.plans.model import (
    PlannedSession,
    PlanStatus,
    Sport,
    TrainingPlan,
)

START = date(2026, 11, 2)


def planned(day: int = 0, **overrides: Any) -> PlannedSession:
    values: dict[str, Any] = {
        "scheduled_date": START + timedelta(days=day),
        "sport": Sport.RUNNING,
        "duration_minutes": 45,
        "intensity_rpe": 6,
    }
    values.update(overrides)
    return PlannedSession(**values)


def create(**overrides: Any) -> TrainingPlan:
    values: dict[str, Any] = {
        "plan_id": uuid4(),
        "athlete_id": uuid4(),
        "professional_id": uuid4(),
        "title": "Base aeróbica",
        "objectives": ["Correr 10 km"],
        "start_date": START,
        "end_date": START + timedelta(days=27),
        "sessions": [planned(0), planned(2)],
        "now": NOW,
    }
    values.update(overrides)
    return TrainingPlan.create(**values)


def violations(**overrides: Any) -> set[Violation]:
    with unittest.TestCase().assertRaises(InvalidTrainingPlan) as raised:
        create(**overrides)
    return set(raised.exception.violations)


class TrainingPlanTest(unittest.TestCase):
    def test_valid_plan_is_assigned_to_athlete_creator_and_period(self) -> None:
        """CA1."""
        athlete, professional = uuid4(), uuid4()
        plan = create(
            athlete_id=athlete,
            professional_id=professional,
            title="  Base   aeróbica ",
            sessions=[planned(3), planned(0)],
        )

        self.assertEqual((plan.athlete_id, plan.professional_id), (athlete, professional))
        self.assertEqual((plan.start_date, plan.end_date), (START, START + timedelta(days=27)))
        self.assertEqual((plan.status, plan.version), (PlanStatus.ASSIGNED, 1))
        self.assertEqual(plan.title, "Base aeróbica")
        self.assertEqual([s.scheduled_date for s in plan.sessions], [START, START + timedelta(3)])

    def test_inverted_dates_are_rejected(self) -> None:
        """CA3: fechas invertidas."""
        found = violations(
            start_date=START, end_date=START - timedelta(days=1), sessions=[planned(0)]
        )

        self.assertIn(Violation("end_date", "end_date_before_start_date"), found)

    def test_period_limit_is_52_weeks_inclusive(self) -> None:
        create(end_date=START + timedelta(days=363))

        self.assertIn(
            Violation("end_date", "period_too_long"),
            violations(end_date=START + timedelta(days=364)),
        )

    def test_incompatible_sessions_are_reported_individually(self) -> None:
        """CA3: sesión fuera del periodo y más de dos sesiones el mismo día."""
        found = violations(sessions=[planned(0), planned(0), planned(0), planned(40)])

        self.assertEqual(
            found,
            {
                Violation("sessions.0.scheduled_date", "too_many_sessions_per_day"),
                Violation("sessions.1.scheduled_date", "too_many_sessions_per_day"),
                Violation("sessions.2.scheduled_date", "too_many_sessions_per_day"),
                Violation("sessions.3.scheduled_date", "outside_plan_period"),
            },
        )

    def test_two_sessions_on_the_same_day_are_allowed(self) -> None:
        plan = create(sessions=[planned(0), planned(0, sport=Sport.STRENGTH)])

        self.assertEqual(len(plan.sessions), 2)

    def test_intensity_and_duration_out_of_range(self) -> None:
        """CA3: intensidad fuera de rango (y duración)."""
        found = violations(
            sessions=[planned(0, intensity_rpe=0), planned(1, intensity_rpe=11, duration_minutes=0)]
        )

        self.assertEqual(
            found,
            {
                Violation("sessions.0.intensity_rpe", "out_of_range"),
                Violation("sessions.1.intensity_rpe", "out_of_range"),
                Violation("sessions.1.duration_minutes", "out_of_range"),
            },
        )

    def test_title_objectives_and_session_count(self) -> None:
        found = violations(title=" ", objectives=[], sessions=[])

        self.assertEqual(
            found,
            {
                Violation("title", "invalid_length"),
                Violation("objectives", "invalid_count"),
                Violation("sessions", "invalid_count"),
            },
        )
        self.assertIn(
            Violation("objectives.1", "invalid_length"),
            violations(objectives=["Correr", "x" * 201]),
        )
        self.assertIn(Violation("objectives", "invalid_count"), violations(objectives=["a"] * 6))
        self.assertIn(
            Violation("sessions", "invalid_count"),
            violations(
                end_date=START + timedelta(days=200), sessions=[planned(i) for i in range(101)]
            ),
        )

    def test_event_payload_has_no_profile_data(self) -> None:
        """CA4: el evento solo lleva identificadores y contenido estructurado del plan."""
        plan = create()
        payload = plan_created_payload(plan)

        self.assertEqual(payload["plan_id"], str(plan.plan_id))
        self.assertEqual(payload["plan_version"], 1)
        self.assertEqual(
            set(payload),
            {
                "plan_id",
                "athlete_id",
                "professional_id",
                "title",
                "objectives",
                "start_date",
                "end_date",
                "status",
                "plan_version",
                "sessions",
                "created_at",
            },
        )
        self.assertEqual(
            set(payload["sessions"][0]),
            {"scheduled_date", "sport", "duration_minutes", "intensity_rpe"},
        )


class TrainingAuthorizationTest(unittest.TestCase):
    def test_grant_revoke_and_reactivate(self) -> None:
        granted = TrainingAuthorization.grant(athlete_id=uuid4(), professional_id=uuid4(), now=NOW)
        revoked = granted.revoke(NOW + timedelta(days=1))
        reactivated = revoked.reactivate(NOW + timedelta(days=2))

        self.assertTrue(granted.is_active)
        self.assertEqual((revoked.status, revoked.version), (AuthorizationStatus.REVOKED, 2))
        self.assertFalse(revoked.is_active)
        self.assertEqual((reactivated.status, reactivated.version), ("ACTIVE", 3))
        self.assertIsNone(reactivated.revoked_at)

    def test_cannot_authorize_oneself(self) -> None:
        user = uuid4()

        with self.assertRaises(SelfAuthorization):
            TrainingAuthorization.grant(athlete_id=user, professional_id=user, now=NOW)


if __name__ == "__main__":
    unittest.main()
