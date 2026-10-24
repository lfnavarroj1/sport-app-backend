"""Componente HU006 en web: BFF → Usuarios → outbox → Pub/Sub → `usuarios-read-projector` →
`athlete_profile_view.sport_habits` → `GET /v1/me/profile` (ADR-034).

Mismo montaje que HU005 (`ProfileFlowCase`): servicios reales con PostgreSQL de escritura y
de lectura migrados y el emulador de Pub/Sub (`TEST_DATABASE_URL`, `PUBSUB_EMULATOR_HOST`).
"""

import json
import unittest
import uuid
from typing import Any

from test_profile_flow import ProfileFlowCase

from affiliation_management.settings import FREE_PLAN_SUBSCRIPTION


def habits(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "sports": [
            {
                "sport": "swimming",
                "frequency_per_week": 2,
                "session_duration_minutes": 60,
                "intensity": "LOW",
                "level": "BEGINNER",
            },
            {
                "sport": "running",
                "frequency_per_week": 3,
                "session_duration_minutes": 45,
                "intensity": "MODERATE",
                "level": "INTERMEDIATE",
                "years_practicing": 4,
            },
        ],
        "availability": {"days": ["SAT", "MON"], "time_slots": ["EVENING"]},
        "primary_goal": "HEALTH",
    }
    values.update(overrides)
    return values


class SportHabitsFlowTest(ProfileFlowCase):
    def put_habits(self, body: Any, session: dict[str, str] | None = None) -> Any:
        session = session or self.athlete
        return self.bff.put(
            "/v1/me/sport-habits",
            json=body,
            headers={
                "Authorization": session["authorization"],
                "Idempotency-Key": str(uuid.uuid4()),
            },
        )

    def test_athlete_registers_and_replaces_habits_and_reads_them_from_the_projection(
        self,
    ) -> None:
        """CA1, CA3 y CA4 de punta a punta; Afiliación no recibe el evento (R-19)."""
        created = self.put_habits(habits())
        replaced = self.put_habits(habits(expected_version=1, primary_goal="PERFORMANCE"))
        stale = self.put_habits(habits(expected_version=1))

        self.assertEqual(created.status_code, 201, created.text)
        self.assertEqual(created.headers["cache-control"], "no-store")
        self.assertEqual([s["sport"] for s in created.json()["sports"]], ["running", "swimming"])
        self.assertEqual(created.json()["availability"]["days"], ["MON", "SAT"])
        self.assertEqual((replaced.status_code, replaced.json()["version"]), (200, 2))
        self.assertEqual(
            (stale.status_code, stale.json()["details"]),
            (409, [{"field": "expected_version", "issue": "2"}]),
        )
        # Registro del deportista y las dos versiones de los hábitos.
        self.assertEqual(self.publish_outbox(), 3)

        projected = self.project_until(
            lambda: self.profile()["versions"]["sport_habits"] == 2  # type: ignore[no-any-return]
        )

        self.assertTrue(projected, "el proyector no aplicó los hábitos")
        view = self.profile()
        self.assertEqual(
            view["sport_habits"], {k: v for k, v in replaced.json().items() if k != "user_id"}
        )
        self.assertEqual(
            view["versions"],
            {
                "demographic": None,
                "physiological": None,
                "sport_habits": 2,
                "nutrition_habits": None,
            },
        )
        free_plan = self.subscriber.pull(
            request={
                "subscription": self.subscriber.subscription_path(
                    self.project, FREE_PLAN_SUBSCRIPTION
                ),
                "max_messages": 10,
            },
            timeout=30,
        ).received_messages
        self.assertEqual(
            {json.loads(item.message.data)["event_type"] for item in free_plan},
            {"usuario.registrado.v1"},
        )
        logged = self.capture.text()
        for value in ("swimming", "EVENING"):
            self.assertNotIn(value, logged)

    def test_invalid_habits_and_other_actors_change_nothing(self) -> None:
        """CA2 y CA5: `422` con `details` sin estado parcial; otro actor recibe `403`."""
        organizer = self.session_for("organizer", "organizador.hu006@example.com")
        body = habits()
        body["sports"].append(dict(body["sports"][0]))

        invalid = self.put_habits(body)
        forbidden = self.put_habits(habits(), organizer)
        with_user_id = self.put_habits(habits(user_id=self.athlete["user_id"]))

        self.assertEqual(invalid.status_code, 422, invalid.text)
        self.assertEqual(
            invalid.json()["details"], [{"field": "sports[2].sport", "issue": "duplicate_value"}]
        )
        self.assertEqual(
            (forbidden.status_code, forbidden.json()["code"]), (403, "permission_required")
        )
        self.assertEqual(
            with_user_id.json()["details"], [{"field": "user_id", "issue": "field_not_allowed"}]
        )
        # Solo los registros del deportista y del organizador: ningún evento de hábitos.
        self.assertEqual(self.publish_outbox(), 2)


if __name__ == "__main__":
    unittest.main()
