"""HU028 (ADR-041): reglas del agregado `TrainingPlanTemplate`, retiro, documento de estado
completo y reglas deterministas de sugerencia (orden con y sin objetivo), sin infraestructura."""

import unittest
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from template_fakes import template_input

from training_management.application.template_documents import (
    InvalidTemplateDocument,
    template_from_document,
)
from training_management.domain.plans.model import Sport
from training_management.domain.templates.events import template_document
from training_management.domain.templates.model import (
    DayOfWeek,
    Goal,
    InvalidTemplate,
    TemplateStatus,
    TrainingPlanTemplate,
    WeeklySession,
)
from training_management.domain.templates.suggestions import (
    TemplateReason,
    TemplateSuggestionContext,
    personalized_order,
    reasons_for,
)

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)


def publish(**overrides: Any) -> TrainingPlanTemplate:
    return TrainingPlanTemplate.publish(
        template_id=uuid4(), professional_id=uuid4(), data=template_input(**overrides), now=NOW
    )


def issues(**overrides: Any) -> list[tuple[str, str]]:
    with unittest.TestCase().assertRaises(InvalidTemplate) as error:
        publish(**overrides)
    return [(item.field, item.issue) for item in error.exception.violations]


class TemplateModelTest(unittest.TestCase):
    def test_valid_template_is_published_in_version_1_normalized(self) -> None:
        template = publish(
            title="  Base   de  10 km ",
            weekly_sessions=(WeeklySession(DayOfWeek.SAT, "  Fondo largo ", 90),),
        )

        self.assertEqual((template.status, template.version), (TemplateStatus.PUBLISHED, 1))
        self.assertEqual(template.title, "Base de 10 km")
        self.assertEqual(template.weekly_sessions[0].description, "Fondo largo")
        self.assertEqual((template.published_at, template.retired_at), (NOW, None))

    def test_field_rules_are_reported_by_field(self) -> None:
        """CA4: validaciones `422` por campo."""
        session = WeeklySession(DayOfWeek.MON, "Rodaje", 40)
        self.assertEqual(
            issues(
                title="ab",
                objectives="corto",
                duration_weeks=53,
                weekly_sessions=(
                    session,
                    WeeklySession(DayOfWeek.TUE, "", 9),
                    WeeklySession(DayOfWeek.WED, "x" * 501, 601),
                ),
            ),
            [
                ("title", "invalid_length"),
                ("objectives", "invalid_length"),
                ("duration_weeks", "out_of_range"),
                ("weekly_sessions.1.description", "invalid_length"),
                ("weekly_sessions.1.duration_minutes", "out_of_range"),
                ("weekly_sessions.2.description", "invalid_length"),
                ("weekly_sessions.2.duration_minutes", "out_of_range"),
            ],
        )
        self.assertEqual(issues(weekly_sessions=()), [("weekly_sessions", "out_of_range")])
        self.assertEqual(
            issues(weekly_sessions=(session,) * 15), [("weekly_sessions", "out_of_range")]
        )
        self.assertEqual(len(publish(weekly_sessions=(session,) * 14).weekly_sessions), 14)
        self.assertEqual(publish(duration_weeks=1, objectives="x" * 2000).duration_weeks, 1)

    def test_retire_bumps_the_version_once_and_keeps_the_template(self) -> None:
        """CA3: `RETIRED` con la versión siguiente; solo una plantilla publicada se retira."""
        template = publish()
        later = NOW + timedelta(days=1)

        retired = template.retire(later)

        self.assertEqual(
            (retired.status, retired.version, retired.retired_at),
            (
                TemplateStatus.RETIRED,
                2,
                later,
            ),
        )
        with self.assertRaises(ValueError):
            retired.retire(later)


class TemplateDocumentTest(unittest.TestCase):
    def test_document_round_trips_and_uses_utc_z(self) -> None:
        template = publish().retire(NOW + timedelta(hours=1))
        document = template_document(template)

        self.assertEqual(template_from_document(document), template)
        self.assertEqual(document["published_at"], "2026-10-24T12:00:00Z")
        self.assertEqual(document["retired_at"], "2026-10-24T13:00:00Z")

    def test_invalid_documents_are_rejected_with_a_reason(self) -> None:
        document = template_document(publish())
        session = document["weekly_sessions"][0]
        cases = {
            "goal_invalid": {"goal": "FUN"},
            "level_invalid": {"level": "PRO"},
            "duration_weeks_invalid": {"duration_weeks": 0},
            "weekly_sessions_invalid": {"weekly_sessions": []},
            "day_of_week_invalid": {"weekly_sessions": [{**session, "day_of_week": "MONDAY"}]},
            "retired_at_mismatch": {"status": "RETIRED"},
            "version_invalid": {"version": True},
            "objectives_invalid": {"objectives": "corto"},
            "created_at_without_offset": {"created_at": "2026-10-24T12:00:00"},
        }
        for reason, change in cases.items():
            with self.subTest(reason=reason), self.assertRaises(InvalidTemplateDocument) as error:
                template_from_document({**document, **change})
            self.assertEqual(error.exception.reason, reason)
        bad_minutes = {**document, "weekly_sessions": [{**session, "duration_minutes": 5}]}
        with self.assertRaises(InvalidTemplateDocument):
            template_from_document(bad_minutes)


class TemplateSuggestionRulesTest(unittest.TestCase):
    def test_reasons_with_and_without_goal(self) -> None:
        """CA1: `SPORT_MATCH` al personalizar y `GOAL_MATCH` si coincide el objetivo."""
        with_goal = TemplateSuggestionContext((Sport.RUNNING,), Goal.HEALTH)
        without_goal = TemplateSuggestionContext((Sport.RUNNING,))
        empty = TemplateSuggestionContext(())

        self.assertEqual(
            reasons_for(with_goal, Goal.HEALTH),
            (TemplateReason.SPORT_MATCH, TemplateReason.GOAL_MATCH),
        )
        self.assertEqual(reasons_for(with_goal, Goal.COMPETITION), (TemplateReason.SPORT_MATCH,))
        self.assertTrue(without_goal.personalized)
        self.assertEqual(reasons_for(without_goal, Goal.HEALTH), (TemplateReason.SPORT_MATCH,))
        self.assertFalse(empty.personalized)
        self.assertEqual(reasons_for(empty, Goal.HEALTH), ())

    def test_order_is_goal_first_then_newest(self) -> None:
        context = TemplateSuggestionContext((Sport.RUNNING,), Goal.HEALTH)
        ids = sorted(uuid4() for _ in range(4))
        candidates = [
            (Goal.COMPETITION, NOW + timedelta(days=3), ids[0]),
            (Goal.HEALTH, NOW, ids[1]),
            (Goal.HEALTH, NOW + timedelta(days=1), ids[2]),
            (Goal.HEALTH, NOW + timedelta(days=1), ids[3]),
        ]

        ordered = sorted(
            candidates,
            key=lambda item: personalized_order(
                context, goal=item[0], published_at=item[1], template_id=item[2]
            ),
        )

        self.assertEqual([item[2] for item in ordered], [ids[3], ids[2], ids[1], ids[0]])
        no_goal = TemplateSuggestionContext((Sport.RUNNING,))
        newest_first = sorted(
            candidates,
            key=lambda item: personalized_order(
                no_goal, goal=item[0], published_at=item[1], template_id=item[2]
            ),
        )
        self.assertEqual(newest_first[0][2], ids[0])


if __name__ == "__main__":
    unittest.main()
