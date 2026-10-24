"""Contrato HTTP de `PUT /v1/me/sport-habits` (HU006, ADR-034) con los casos de uso reales y
persistencia en memoria; el token se firma y se verifica de verdad (mismo armado que HU005)."""

import logging
import unittest
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from fakes import (
    FixedClock,
    InMemoryProfileReader,
    InMemoryProfileStore,
    patch_sport_habits_with,
    sport_habits_body,
    update_demographic_with,
    update_nutrition_habits_with,
    update_physiological_with,
    update_sport_habits_with,
)
from fastapi.testclient import TestClient
from test_profile_api import PROFILE, VERIFIER, bearer

from sportapp_platform.app import create_service_app
from sportapp_platform.config import ServiceSettings
from users_management.api.profile import build_profile_router
from users_management.application.ports import AthleteProfileView
from users_management.application.queries.get_profile import GetAthleteProfile

SPORT_HABITS = "/v1/me/sport-habits"
# Valores sintéticos que no pueden aparecer en un UUID, un instante ni una duración.
SENSITIVE_VALUES = ("swimming", "EVENING", "INTERMEDIATE")


class SportHabitsApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = InMemoryProfileStore()
        self.reader = InMemoryProfileReader()
        clock = FixedClock()
        app = create_service_app(
            service_name="users-management",
            title="t",
            api_version="0",
            settings=ServiceSettings(app_env="test"),  # type: ignore[call-arg]
            routers=[
                build_profile_router(
                    verify_token=VERIFIER.verify,
                    update_demographic=update_demographic_with(self.store, clock),
                    update_physiological=update_physiological_with(self.store, clock),
                    update_sport_habits=update_sport_habits_with(self.store, clock),
                    patch_sport_habits=patch_sport_habits_with(self.store, clock),
                    update_nutrition_habits=update_nutrition_habits_with(self.store, clock),
                    get_profile=GetAthleteProfile(self.reader),
                )
            ],
        )
        self.client = TestClient(app)
        self.user_id = uuid4()
        self.auth = bearer(self.user_id)

    def habits(self, body: Any, key: str | None = "new", auth: dict[str, str] | None = None) -> Any:
        headers = dict(self.auth if auth is None else auth)
        if key == "new":
            key = str(uuid4())
        if key is not None:
            headers["Idempotency-Key"] = key
        return self.client.put(SPORT_HABITS, json=body, headers=headers)

    def details(self, response: Any) -> list[tuple[str | None, str]]:
        return [(item["field"], item["issue"]) for item in response.json()["details"]]

    # --- CA1: alta, reemplazo y normalización ---

    def test_sport_habits_201_then_200_normalized(self) -> None:
        """CA1: `201` al crear y `200` al reemplazar, con el estado normalizado y su versión."""
        created = self.habits(sport_habits_body())

        self.assertEqual(created.status_code, 201, created.text)
        self.assertEqual(created.headers["Cache-Control"], "no-store")
        self.assertEqual(
            created.json(),
            {
                "user_id": str(self.user_id),
                "version": 1,
                "sports": [
                    {
                        "sport": "running",
                        "frequency_per_week": 3,
                        "session_duration_minutes": 45,
                        "intensity": "MODERATE",
                        "level": "INTERMEDIATE",
                        "years_practicing": 4,
                    },
                    {
                        "sport": "swimming",
                        "frequency_per_week": 2,
                        "session_duration_minutes": 60,
                        "intensity": "LOW",
                        "level": "BEGINNER",
                        "years_practicing": None,
                    },
                ],
                "availability": {
                    "days": ["MON", "WED", "SAT"],
                    "time_slots": ["MORNING", "EVENING"],
                },
                "primary_goal": "HEALTH",
                "created_at": "2026-10-07T12:00:00Z",
                "updated_at": "2026-10-07T12:00:00Z",
            },
        )

        replaced = self.habits(sport_habits_body(expected_version=1, primary_goal=None))

        self.assertEqual(replaced.status_code, 200, replaced.text)
        self.assertEqual(replaced.json()["version"], 2)
        self.assertIsNone(replaced.json()["primary_goal"])
        self.assertEqual(replaced.json()["created_at"], "2026-10-07T12:00:00Z")

    def test_successful_command_publishes_the_complete_section_once(self) -> None:
        """CA3: un evento con el estado completo y sin datos demográficos ni fisiológicos."""
        created = self.habits(sport_habits_body()).json()

        [message] = self.store.outbox
        self.assertEqual(
            (message.event_type, message.aggregate_type, message.aggregate_id),
            ("habitos.deportivos_actualizados.v1", "sport_habits", self.user_id),
        )
        self.assertEqual((message.aggregate_version, message.schema_version), (1, 1))
        self.assertEqual(message.payload, created)
        self.assertFalse({"birth_date", "weight_kg", "bmi"} & set(message.payload))

    # --- CA2: validación todo o nada ---

    def test_invalid_body_keeps_the_previous_state(self) -> None:
        """CA2: `422 validation_error` con `details` y sin nueva versión ni evento."""
        self.habits(sport_habits_body())
        body = sport_habits_body(expected_version=1)
        body["sports"].append(dict(body["sports"][1]))  # deporte repetido

        response = self.habits(body)

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["code"], "validation_error")
        self.assertEqual(self.details(response), [("sports[2].sport", "duplicate_value")])
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(len(self.store.outbox), 1)

    def test_user_id_in_the_body_is_rejected(self) -> None:
        """Seguridad: el deportista sale del token; `user_id` en el cuerpo → `422`."""
        response = self.habits(sport_habits_body(user_id=str(uuid4())))

        self.assertEqual(self.details(response), [("user_id", "field_not_allowed")])
        self.assertEqual(self.store.outbox, [])

    def test_key_problems_are_reported_with_body_violations(self) -> None:
        missing = self.habits(sport_habits_body(primary_goal="FUN"), key=None)
        invalid = self.habits(sport_habits_body(), key="no-es-uuid")

        self.assertEqual(
            self.details(missing),
            [("Idempotency-Key", "required"), ("primary_goal", "unknown_value")],
        )
        self.assertEqual(self.details(invalid), [("Idempotency-Key", "unknown_value")])

    # --- CA4: idempotencia y versión ---

    def test_retry_with_same_key_returns_the_original(self) -> None:
        key = str(uuid4())
        first = self.habits(sport_habits_body(), key=key)
        reordered = sport_habits_body()
        reordered["sports"].reverse()  # la misma información en otro orden

        retry = self.habits(reordered, key=key)

        self.assertEqual((retry.status_code, retry.json()), (201, first.json()))
        self.assertEqual(retry.headers["Idempotent-Replayed"], "true")
        self.assertEqual(len(self.store.outbox), 1)

    def test_key_reused_with_other_payload_or_athlete(self) -> None:
        key = str(uuid4())
        self.habits(sport_habits_body(), key=key)

        other_payload = self.habits(sport_habits_body(primary_goal="COMPETITION"), key=key)
        other_athlete = self.habits(sport_habits_body(), key=key, auth=bearer(uuid4()))

        for response in (other_payload, other_athlete):
            self.assertEqual(
                (response.status_code, response.json()["code"]), (409, "idempotency_key_reused")
            )
        self.assertEqual(len(self.store.outbox), 1)

    def test_stale_or_missing_expected_version_is_a_conflict(self) -> None:
        """CA4: `409 version_conflict` con la versión vigente (`0` si no existe)."""
        premature = self.habits(sport_habits_body(expected_version=1))
        self.habits(sport_habits_body())
        missing = self.habits(sport_habits_body(primary_goal="RECREATION"))
        stale = self.habits(sport_habits_body(expected_version=3))

        for response, issue in ((premature, "0"), (missing, "1"), (stale, "1")):
            self.assertEqual(response.json()["code"], "version_conflict")
            self.assertEqual(self.details(response), [("expected_version", issue)])
        self.assertEqual(len(self.store.outbox), 1)

    # --- CA5: autorización y aislamiento ---

    def test_only_athletes_and_only_their_own_section(self) -> None:
        """CA5: otros actores → `403 permission_required`; sin token → `401`."""
        for actor in ("organizer", "service_provider"):
            with self.subTest(actor=actor):
                response = self.habits(sport_habits_body(), auth=bearer(uuid4(), actor))
                self.assertEqual(
                    (response.status_code, response.json()["code"]), (403, "permission_required")
                )
        unauthenticated = self.habits(sport_habits_body(), auth={})
        self.assertEqual(unauthenticated.status_code, 401)

        other: UUID = uuid4()
        self.habits(sport_habits_body(), auth=bearer(other))
        mine = self.habits(sport_habits_body())

        self.assertEqual((mine.status_code, mine.json()["version"]), (201, 1))
        self.assertEqual(mine.json()["user_id"], str(self.user_id))

    def test_profile_query_returns_the_projected_habits(self) -> None:
        """CA1: la consulta del perfil refleja la sección una vez proyectada."""
        section = {
            k: v for k, v in self.habits(sport_habits_body()).json().items() if k != "user_id"
        }
        self.reader.views[self.user_id] = AthleteProfileView(
            user_id=self.user_id,
            demographic=None,
            physiological=None,
            sport_habits=section,
            nutrition_habits=None,
            demographic_version=None,
            physiological_version=None,
            sport_habits_version=1,
            nutrition_habits_version=None,
            projected_at=datetime(2026, 10, 7, 12, 0, 1, tzinfo=UTC),
        )

        body = self.client.get(PROFILE, headers=self.auth).json()

        self.assertEqual(body["sport_habits"], section)
        self.assertEqual(body["versions"]["sport_habits"], 1)

    def test_logs_and_audit_never_contain_habits(self) -> None:
        with self.assertLogs(logging.getLogger(), level="DEBUG") as logs:
            self.habits(sport_habits_body())
            self.habits(sport_habits_body(primary_goal="FUN"))

        rendered = " ".join(logs.output) + " ".join(
            str(getattr(record, "fields", "")) for record in logs.records
        )
        for value in SENSITIVE_VALUES:
            self.assertNotIn(value, rendered)
        audit = [
            record.fields  # type: ignore[attr-defined]
            for record in logs.records
            if record.name == "sportapp.audit"
        ]
        self.assertEqual(
            [(entry["section"], entry["result"]) for entry in audit],
            [("sport_habits", "created"), ("sport_habits", "rejected")],
        )


if __name__ == "__main__":
    unittest.main()
