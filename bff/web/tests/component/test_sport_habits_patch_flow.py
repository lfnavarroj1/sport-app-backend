"""Componente HU008 en web: `PATCH /v1/me/sport-habits` por el BFF → Usuarios → outbox →
Pub/Sub → `usuarios-read-projector` → `athlete_profile_view.sport_habits` →
`GET /v1/me/profile` (ADR-035).

Mismo montaje que HU005/HU006 (`ProfileFlowCase`): servicios reales con PostgreSQL de
escritura y de lectura migrados y el emulador de Pub/Sub (`TEST_DATABASE_URL`,
`PUBSUB_EMULATOR_HOST`).
"""

import unittest
import uuid
from typing import Any

from test_profile_flow import ProfileFlowCase
from test_sport_habits_flow import habits

CYCLING = {
    "sport": "cycling",
    "frequency_per_week": 4,
    "session_duration_minutes": 90,
    "intensity": "HIGH",
    "level": "ADVANCED",
}


class SportHabitsPatchFlowTest(ProfileFlowCase):
    def send(self, method: str, body: Any, session: dict[str, str] | None = None) -> Any:
        session = session or self.athlete
        return self.bff.request(
            method,
            "/v1/me/sport-habits",
            json=body,
            headers={
                "Authorization": session["authorization"],
                "Idempotency-Key": str(uuid.uuid4()),
            },
        )

    def test_athlete_edits_one_section_keeps_the_rest_and_reads_it_from_the_projection(
        self,
    ) -> None:
        """CA1-CA4 de punta a punta: cada sección se reemplaza sola, las demás se conservan y
        el evento con el estado completo llega a la vista del perfil."""
        created = self.send("PUT", habits())
        sports = self.send("PATCH", {"expected_version": 1, "sports": [CYCLING]})
        goal = self.send("PATCH", {"expected_version": 2, "primary_goal": None})
        stale = self.send("PATCH", {"expected_version": 2, "primary_goal": "COMPETITION"})

        self.assertEqual(created.status_code, 201, created.text)
        self.assertEqual((sports.status_code, sports.json()["version"]), (200, 2), sports.text)
        self.assertEqual([item["sport"] for item in sports.json()["sports"]], ["cycling"])
        self.assertEqual(sports.json()["availability"], created.json()["availability"])
        self.assertEqual(sports.json()["primary_goal"], "HEALTH")
        self.assertEqual(sports.headers["cache-control"], "no-store")
        self.assertEqual((goal.status_code, goal.json()["primary_goal"]), (200, None))
        self.assertEqual(goal.json()["sports"], sports.json()["sports"])
        self.assertEqual(
            (stale.status_code, stale.json()["details"]),
            (409, [{"field": "expected_version", "issue": "3"}]),
        )
        # Registro del deportista, el alta de HU006 y las dos ediciones.
        self.assertEqual(self.publish_outbox(), 4)

        projected = self.project_until(
            lambda: self.profile()["versions"]["sport_habits"] == 3  # type: ignore[no-any-return]
        )

        self.assertTrue(projected, "el proyector no aplicó la edición")
        view = self.profile()
        self.assertEqual(
            view["sport_habits"], {k: v for k, v in goal.json().items() if k != "user_id"}
        )
        logged = self.capture.text()
        for value in ("cycling", "EVENING"):
            self.assertNotIn(value, logged)

    def test_rejected_patches_change_nothing(self) -> None:
        """CA5 y seguridad: sin hábitos `404`; sin secciones `422 required`; `user_id` `422`;
        otro actor `403`. Ningún evento de hábitos."""
        organizer = self.session_for("organizer", "organizador.hu008@example.com")

        missing = self.send("PATCH", {"expected_version": 1, "primary_goal": "HEALTH"})
        self.send("PUT", habits())
        empty = self.send("PATCH", {"expected_version": 1})
        with_user_id = self.send(
            "PATCH",
            {"expected_version": 1, "primary_goal": "HEALTH", "user_id": self.athlete["user_id"]},
        )
        duplicated = self.send("PATCH", {"expected_version": 1, "sports": [CYCLING, dict(CYCLING)]})
        forbidden = self.send("PATCH", {"expected_version": 1, "primary_goal": None}, organizer)

        self.assertEqual(
            (missing.status_code, missing.json()["code"]), (404, "sport_habits_not_found")
        )
        self.assertEqual(
            (empty.status_code, empty.json()["details"]),
            (422, [{"field": None, "issue": "required"}]),
        )
        self.assertEqual(
            with_user_id.json()["details"], [{"field": "user_id", "issue": "field_not_allowed"}]
        )
        self.assertEqual(
            duplicated.json()["details"], [{"field": "sports[1].sport", "issue": "duplicate_value"}]
        )
        self.assertEqual(
            (forbidden.status_code, forbidden.json()["code"]), (403, "permission_required")
        )
        # Registros del deportista y del organizador y el alta de HU006: ninguna edición.
        self.assertEqual(self.publish_outbox(), 3)


if __name__ == "__main__":
    unittest.main()
