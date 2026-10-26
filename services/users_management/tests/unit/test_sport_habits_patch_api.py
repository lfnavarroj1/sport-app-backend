"""Contrato HTTP de `PATCH /v1/me/sport-habits` (HU008, ADR-035) con los casos de uso reales
y persistencia en memoria; el token se firma y se verifica de verdad (armado de HU006)."""

import logging
import unittest
from typing import Any
from uuid import uuid4

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
from test_profile_api import VERIFIER, bearer
from test_sport_habits_api import SPORT_HABITS

from sportapp_platform.app import create_service_app
from sportapp_platform.config import ServiceSettings
from users_management.api.profile import build_profile_router
from users_management.application.queries.get_profile import GetAthleteProfile

CYCLING: dict[str, Any] = {
    "sport": "cycling",
    "frequency_per_week": 4,
    "session_duration_minutes": 90,
    "intensity": "HIGH",
    "level": "ADVANCED",
}
# Valores sintéticos que no pueden aparecer en un UUID, un instante ni una duración.
SENSITIVE_VALUES = ("cycling", "swimming", "COMPETITION", "EVENING")


class SportHabitsPatchApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = InMemoryProfileStore()
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
                    get_profile=GetAthleteProfile(InMemoryProfileReader()),
                )
            ],
        )
        self.client = TestClient(app)
        self.user_id = uuid4()
        self.auth = bearer(self.user_id)

    def habits(self, body: Any, key: str | None = "new") -> Any:
        headers = {**self.auth, "Idempotency-Key": key or str(uuid4())}
        if key == "new":
            headers["Idempotency-Key"] = str(uuid4())
        return self.client.put(SPORT_HABITS, json=body, headers=headers)

    def details(self, response: Any) -> list[tuple[str | None, str]]:
        return [(item["field"], item["issue"]) for item in response.json()["details"]]

    def patch(self, body: Any, key: str | None = "new", auth: dict[str, str] | None = None) -> Any:
        headers = dict(self.auth if auth is None else auth)
        if key == "new":
            key = str(uuid4())
        if key is not None:
            headers["Idempotency-Key"] = key
        return self.client.patch(SPORT_HABITS, json=body, headers=headers)

    def registered(self) -> dict[str, Any]:
        response = self.habits(sport_habits_body())
        self.assertEqual(response.status_code, 201, response.text)
        result: dict[str, Any] = response.json()
        return result

    # --- CA1: secciones enviadas reemplazadas, el resto conservado ---

    def test_patch_one_section_keeps_the_others(self) -> None:
        """CA1: `200` con el estado completo, la versión nueva y lo no enviado intacto."""
        current = self.registered()

        response = self.patch({"expected_version": 1, "sports": [CYCLING]})

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        body = response.json()
        self.assertEqual(body["version"], 2)
        self.assertEqual(body["sports"], [{**CYCLING, "years_practicing": None}])
        self.assertEqual(body["availability"], current["availability"])
        self.assertEqual(body["primary_goal"], current["primary_goal"])
        self.assertEqual(body["user_id"], str(self.user_id))
        self.assertEqual(body["created_at"], current["created_at"])
        self.assertNotIn("Idempotent-Replayed", response.headers)

    def test_successive_patches_accumulate(self) -> None:
        current = self.registered()

        goal = self.patch({"expected_version": 1, "primary_goal": "COMPETITION"}).json()
        availability = self.patch(
            {"expected_version": 2, "availability": {"days": ["SUN"], "time_slots": ["NIGHT"]}}
        ).json()
        cleared = self.patch({"expected_version": 3, "primary_goal": None}).json()

        self.assertEqual(goal["primary_goal"], "COMPETITION")
        self.assertEqual(availability["primary_goal"], "COMPETITION")
        self.assertEqual(availability["availability"], {"days": ["SUN"], "time_slots": ["NIGHT"]})
        self.assertEqual(cleared["version"], 4)
        self.assertIsNone(cleared["primary_goal"])
        self.assertEqual(cleared["sports"], current["sports"])
        self.assertEqual(cleared["availability"], availability["availability"])

    # --- CA2: concurrencia ---

    def test_stale_expected_version_is_a_conflict_without_losing_changes(self) -> None:
        """CA2: `409 version_conflict` con la versión vigente; el estado no cambia."""
        self.registered()
        first = self.patch({"expected_version": 1, "primary_goal": "COMPETITION"})

        stale = self.patch({"expected_version": 1, "primary_goal": "RECREATION"})

        self.assertEqual((stale.status_code, stale.json()["code"]), (409, "version_conflict"))
        self.assertEqual(self.details(stale), [("expected_version", "2")])
        self.assertEqual(stale.headers["Cache-Control"], "no-store")
        self.assertEqual(len(self.store.outbox), 2)
        self.assertEqual(self.store.outbox[-1].payload, first.json())

    def test_missing_section_is_not_found(self) -> None:
        """Precondición: sin hábitos de HU006 → `404 sport_habits_not_found`; nada se crea."""
        response = self.patch({"expected_version": 1, "primary_goal": "HEALTH"})

        self.assertEqual(
            (response.status_code, response.json()["code"]), (404, "sport_habits_not_found")
        )
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(self.store.outbox, [])
        self.assertEqual(self.store.sections, {})
        self.assertEqual(self.store.idempotency, {})

    # --- CA3: evento, idempotencia y auditoría ---

    def test_successful_patch_publishes_the_complete_state_once(self) -> None:
        """CA3: mismo evento de HU006, versión nueva y estado completo."""
        self.registered()
        body = self.patch({"expected_version": 1, "sports": [CYCLING]}).json()

        message = self.store.outbox[-1]
        self.assertEqual(len(self.store.outbox), 2)
        self.assertEqual(
            (message.event_type, message.aggregate_type, message.aggregate_id),
            ("habitos.deportivos_actualizados.v1", "sport_habits", self.user_id),
        )
        self.assertEqual((message.aggregate_version, message.schema_version), (2, 1))
        self.assertEqual(message.payload, body)

    def test_retry_with_same_key_returns_the_original(self) -> None:
        """CA3: la misma `Idempotency-Key` devuelve la respuesta original sin otro evento."""
        self.registered()
        key = str(uuid4())
        first = self.patch({"expected_version": 1, "sports": [CYCLING]}, key=key)

        retry = self.patch({"expected_version": 1, "sports": [CYCLING]}, key=key)

        self.assertEqual((retry.status_code, retry.json()), (200, first.json()))
        self.assertEqual(retry.headers["Idempotent-Replayed"], "true")
        self.assertEqual(len(self.store.outbox), 2)

    def test_key_reused_with_other_payload_athlete_or_operation(self) -> None:
        self.registered()
        key = str(uuid4())
        self.patch({"expected_version": 1, "primary_goal": "COMPETITION"}, key=key)

        other_payload = self.patch({"expected_version": 1, "primary_goal": None}, key=key)
        other_athlete = self.patch(
            {"expected_version": 1, "primary_goal": "COMPETITION"}, key=key, auth=bearer(uuid4())
        )
        other_operation = self.habits(sport_habits_body(expected_version=2), key=key)

        for response in (other_payload, other_athlete, other_operation):
            self.assertEqual(
                (response.status_code, response.json()["code"]), (409, "idempotency_key_reused")
            )
        self.assertEqual(len(self.store.outbox), 2)

    def test_logs_and_audit_never_contain_habits(self) -> None:
        """CA3: auditoría `audit.profile_section_updated` sin valores."""
        self.registered()
        with self.assertLogs(logging.getLogger(), level="DEBUG") as logs:
            self.patch({"expected_version": 1, "sports": [CYCLING], "primary_goal": "COMPETITION"})
            self.patch({"expected_version": 1, "primary_goal": "COMPETITION"})
            self.patch({"expected_version": 2, "primary_goal": "FUN"})

        rendered = " ".join(logs.output) + " ".join(
            str(getattr(record, "fields", "")) for record in logs.records
        )
        for value in SENSITIVE_VALUES:
            self.assertNotIn(value, rendered)
        audit = [
            record.fields  # type: ignore[attr-defined]
            for record in logs.records
            if record.name == "sportapp.audit" and record.msg == "audit.profile_section_updated"
        ]
        self.assertEqual(
            [
                (entry["section"], entry["section_version"], entry["result"], entry.get("reason"))
                for entry in audit
            ],
            [
                ("sport_habits", 2, "updated", None),
                ("sport_habits", None, "rejected", "version_conflict"),
                ("sport_habits", None, "rejected", "validation_error"),
            ],
        )

    # --- CA5: validación todo o nada ---

    def test_empty_or_sectionless_body_is_rejected_and_state_kept(self) -> None:
        """CA5: sin secciones → `422 required` (`field: null`); se conserva el estado."""
        current = self.registered()
        headers = {**self.auth, "Idempotency-Key": str(uuid4())}

        empty = self.client.patch(SPORT_HABITS, content=b"", headers=headers)
        sectionless = self.patch({"expected_version": 1})
        malformed = self.client.patch(SPORT_HABITS, content=b"{no", headers=headers)

        for response, expected in (
            (empty, [(None, "required")]),
            (sectionless, [(None, "required")]),
            (malformed, [(None, "unknown_value")]),
        ):
            self.assertEqual(response.status_code, 422)
            self.assertEqual(response.json()["code"], "validation_error")
            self.assertEqual(self.details(response), expected)
        self.assertEqual(len(self.store.outbox), 1)
        self.assertEqual(self.store.outbox[0].payload, current)

    def test_values_outside_the_catalogs_keep_the_state(self) -> None:
        """CA5: un valor fuera de catálogo invalida todo el cuerpo (todo o nada)."""
        self.registered()

        response = self.patch({"expected_version": 1, "sports": [CYCLING], "primary_goal": "FUN"})

        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.details(response), [("primary_goal", "unknown_value")])
        self.assertEqual(len(self.store.outbox), 1)

    def test_missing_expected_version_is_required(self) -> None:
        self.registered()

        response = self.patch({"primary_goal": "COMPETITION"})

        self.assertEqual(self.details(response), [("expected_version", "required")])

    def test_key_problems_are_reported_with_body_violations(self) -> None:
        self.registered()
        missing = self.patch({"expected_version": 1, "primary_goal": "FUN"}, key=None)
        invalid = self.patch({"expected_version": 1, "primary_goal": None}, key="no-es-uuid")

        self.assertEqual(
            self.details(missing),
            [("Idempotency-Key", "required"), ("primary_goal", "unknown_value")],
        )
        self.assertEqual(self.details(invalid), [("Idempotency-Key", "unknown_value")])

    # --- Seguridad ---

    def test_user_id_in_the_body_is_rejected(self) -> None:
        """Seguridad: el deportista sale del token; `user_id` en el cuerpo → `422`."""
        self.registered()

        response = self.patch(
            {"expected_version": 1, "primary_goal": None, "user_id": str(uuid4())}
        )

        self.assertEqual(self.details(response), [("user_id", "field_not_allowed")])
        self.assertEqual(len(self.store.outbox), 1)

    def test_only_athletes_and_only_their_own_section(self) -> None:
        """Otros actores → `403 permission_required`; sin token → `401`; cada deportista
        edita solo su sección."""
        self.registered()
        for actor in ("organizer", "service_provider"):
            with self.subTest(actor=actor):
                response = self.patch(
                    {"expected_version": 1, "primary_goal": None}, auth=bearer(uuid4(), actor)
                )
                self.assertEqual(
                    (response.status_code, response.json()["code"]), (403, "permission_required")
                )
        unauthenticated = self.patch({"expected_version": 1, "primary_goal": None}, auth={})
        other = self.patch({"expected_version": 1, "primary_goal": None}, auth=bearer(uuid4()))

        self.assertEqual(unauthenticated.status_code, 401)
        self.assertEqual((other.status_code, other.json()["code"]), (404, "sport_habits_not_found"))
        self.assertEqual(len(self.store.outbox), 1)


if __name__ == "__main__":
    unittest.main()
