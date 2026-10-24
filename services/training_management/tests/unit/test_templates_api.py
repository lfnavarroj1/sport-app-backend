"""HU028 (ADR-041): API del servicio para plantillas de plan (autenticación, autorización,
validación por campo, idempotencia, retiro, consulta propia y sugerencias) con routers reales,
casos de uso con dobles en memoria y tokens EdDSA firmados en la prueba."""

import unittest
import uuid
from datetime import timedelta
from typing import Any

from fakes import FakeCapabilities, FakePermissions, FixedClock
from fastapi.testclient import TestClient
from template_fakes import (
    AUTHOR,
    SUGGEST,
    InMemoryTemplateReader,
    InMemoryTemplateStore,
    InMemoryTemplateUnitOfWork,
    template_body,
)
from test_api import VERIFIER, token

from sportapp_platform.app import create_service_app
from sportapp_platform.config import ServiceSettings
from training_management.api.training_plan_templates import build_training_plan_templates_router
from training_management.application.commands.training_plan_templates import (
    PublishTemplate,
    RetireTemplate,
)
from training_management.application.errors import CapabilitiesTimeout
from training_management.application.queries.training_plan_templates import (
    ListMyTemplates,
    SuggestTemplates,
)
from training_management.domain.templates.events import (
    PUBLISHED_EVENT_TYPE,
    RETIRED_EVENT_TYPE,
)

BASE = "/v1/training-plan-templates"
SUGGESTIONS = "/v1/me/suggestions/training-plan-templates"


class _SlowCapabilities(FakeCapabilities):
    def has_capability(self, user_id: uuid.UUID, capability: str) -> bool:
        raise CapabilitiesTimeout()


def _bearer(user_id: uuid.UUID, actor_type: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token(user_id, actor_type)}"}


class TemplatesApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = InMemoryTemplateStore()
        self.clock = FixedClock()
        self.professional_id, self.athlete_id = uuid.uuid4(), uuid.uuid4()
        self.capabilities = FakeCapabilities({self.athlete_id: {SUGGEST}})
        self.reader = InMemoryTemplateReader(self.store.templates)
        self.client = self.client_with(self.capabilities)
        self.professional = _bearer(self.professional_id, "service_provider")
        self.athlete = _bearer(self.athlete_id, "athlete")

    def client_with(self, capabilities: FakeCapabilities) -> TestClient:
        permissions = FakePermissions({"service_provider": {AUTHOR}})
        app = create_service_app(
            service_name="training-management",
            title="test",
            api_version="test",
            settings=ServiceSettings(app_env="test"),  # type: ignore[call-arg]
            routers=[
                build_training_plan_templates_router(
                    verify_token=VERIFIER.verify,
                    publish=PublishTemplate(
                        unit_of_work=lambda: InMemoryTemplateUnitOfWork(self.store),
                        permissions=permissions,
                        clock=self.clock,
                    ),
                    retire=RetireTemplate(
                        unit_of_work=lambda: InMemoryTemplateUnitOfWork(self.store),
                        permissions=permissions,
                        clock=self.clock,
                    ),
                    list_templates=ListMyTemplates(reader=self.reader, permissions=permissions),
                    suggest=SuggestTemplates(reader=self.reader, capabilities=capabilities),
                )
            ],
        )
        return TestClient(app)

    def publish(
        self, body: dict[str, Any] | None = None, key: str | None = None, headers: Any = None
    ) -> Any:
        return self.client.post(
            BASE,
            json=body or template_body(),
            headers={
                **(headers or self.professional),
                "Idempotency-Key": key or str(uuid.uuid4()),
            },
        )

    def retire(self, template_id: str, version: int = 1, headers: Any = None) -> Any:
        return self.client.post(
            f"{BASE}/{template_id}/retire",
            json={"expected_version": version},
            headers=headers or self.professional,
        )

    def test_publishes_idempotently_with_location_and_event(self) -> None:
        """CA4: `201` `PUBLISHED` v1; el reintento no publica otra; otra plantilla → `409`."""
        key = str(uuid.uuid4())
        with self.assertLogs("sportapp.audit", level="INFO") as audit:
            first = self.publish(key=key)
        again = self.publish(key=key)
        other = self.publish(template_body(title="Otra plantilla"), key=key)

        self.assertEqual(first.status_code, 201, first.text)
        body = first.json()
        self.assertEqual(first.headers["Location"], f"{BASE}/{body['template_id']}")
        self.assertEqual(
            (body["status"], body["version"], body["professional_id"]),
            ("PUBLISHED", 1, str(self.professional_id)),
        )
        self.assertEqual(len(body["weekly_sessions"]), 2)
        self.assertEqual(body["published_at"], "2026-10-12T12:00:00Z")
        self.assertEqual((again.json(), again.headers["Idempotent-Replayed"]), (body, "true"))
        self.assertEqual((other.status_code, other.json()["code"]), (409, "idempotency_key_reused"))
        [event] = self.store.outbox
        self.assertEqual(
            (event.event_type, event.aggregate_type, event.payload),
            (PUBLISHED_EVENT_TYPE, "training_plan_template", body),
        )
        self.assertNotIn("Base de 10 km", " ".join(audit.output))

    def test_invalid_templates_are_422_by_field_and_nothing_is_published(self) -> None:
        """CA4: validaciones `422` por campo."""
        missing = template_body()
        del missing["goal"]
        cases: dict[str, tuple[dict[str, Any], tuple[str, str]]] = {
            "objetivo faltante": (missing, ("body.goal", "missing")),
            "título corto": (template_body(title="ab"), ("body.title", "string_too_short")),
            "objetivos cortos": (
                template_body(objectives="corto"),
                ("body.objectives", "string_too_short"),
            ),
            "semanas": (
                template_body(duration_weeks=53),
                ("body.duration_weeks", "less_than_equal"),
            ),
            "sin sesiones": (
                template_body(weekly_sessions=[]),
                ("body.weekly_sessions", "too_short"),
            ),
            "minutos": (
                template_body(
                    weekly_sessions=[
                        {"day_of_week": "MON", "description": "x", "duration_minutes": 5}
                    ]
                ),
                ("body.weekly_sessions.0.duration_minutes", "greater_than_equal"),
            ),
            "día": (
                template_body(
                    weekly_sessions=[
                        {"day_of_week": "LUNES", "description": "x", "duration_minutes": 30}
                    ]
                ),
                ("body.weekly_sessions.0.day_of_week", "enum"),
            ),
            "nivel": (template_body(level="PRO"), ("body.level", "enum")),
            "deportista": (
                template_body(athlete_id=str(uuid.uuid4())),
                ("body.athlete_id", "extra_forbidden"),
            ),
        }
        for name, (body, expected) in cases.items():
            with self.subTest(name=name):
                response = self.publish(body)

                self.assertEqual(response.status_code, 422, response.text)
                self.assertEqual(response.json()["code"], "validation_error")
                self.assertIn(
                    expected,
                    {(item["field"], item["issue"]) for item in response.json()["details"]},
                )
        self.assertEqual((self.store.templates, self.store.outbox), ({}, []))

    def test_only_plan_authors_publish_retire_and_list(self) -> None:
        for actor in ("athlete", "organizer"):
            headers = _bearer(uuid.uuid4(), actor)
            with self.subTest(actor=actor):
                for response in (
                    self.publish(headers=headers),
                    self.retire(str(uuid.uuid4()), headers=headers),
                    self.client.get("/v1/me/training-plan-templates", headers=headers),
                ):
                    self.assertEqual(
                        (response.status_code, response.json()["code"]),
                        (403, "permission_required"),
                    )
        self.assertEqual(
            self.client.post(
                BASE, json=template_body(), headers={"Idempotency-Key": str(uuid.uuid4())}
            ).status_code,
            401,
        )

    def test_retire_foreign_stale_and_repeated(self) -> None:
        """CA3: ajena → `404 template_not_found`; versión vieja → `409`; repetir → `200`."""
        template_id = self.publish().json()["template_id"]
        self.clock.advance(timedelta(hours=1))

        foreign = self.retire(template_id, headers=_bearer(uuid.uuid4(), "service_provider"))
        stale = self.retire(template_id, version=2)
        retired = self.retire(template_id)
        again = self.retire(template_id)

        self.assertEqual((foreign.status_code, foreign.json()["code"]), (404, "template_not_found"))
        self.assertEqual(
            (stale.status_code, stale.json()["details"]),
            (409, [{"field": "expected_version", "issue": "1"}]),
        )
        self.assertEqual((retired.json()["status"], retired.json()["version"]), ("RETIRED", 2))
        self.assertEqual(again.json(), retired.json())
        self.assertEqual(
            [event.event_type for event in self.store.outbox],
            [PUBLISHED_EVENT_TYPE, RETIRED_EVENT_TYPE],
        )
        self.assertEqual(self.retire(str(uuid.uuid4())).status_code, 404)

    def test_my_templates_pages_without_losing_or_repeating(self) -> None:
        ids = []
        for _ in range(5):
            self.clock.advance(timedelta(minutes=1))
            ids.append(self.publish().json()["template_id"])
        self.publish(headers=_bearer(uuid.uuid4(), "service_provider"))

        seen, cursor = [], None
        while True:
            params = {"limit": "2", **({"cursor": cursor} if cursor else {})}
            response = self.client.get(
                "/v1/me/training-plan-templates", params=params, headers=self.professional
            )
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            seen.extend(item["template_id"] for item in response.json()["items"])
            cursor = response.json()["next_cursor"]
            if cursor is None:
                break

        self.assertEqual(seen, list(reversed(ids)))
        invalid = self.client.get(
            "/v1/me/training-plan-templates", params={"cursor": "x"}, headers=self.professional
        )
        self.assertEqual(
            invalid.json()["details"][0], {"field": "query.cursor", "issue": "invalid_cursor"}
        )

    def seed(self) -> dict[str, str]:
        names = {}
        for title, sport, goal in (
            ("Salud running", "running", "HEALTH"),
            ("Competencia running", "running", "COMPETITION"),
            ("Salud ciclismo", "cycling", "HEALTH"),
            ("Salud natación", "swimming", "HEALTH"),
        ):
            self.clock.advance(timedelta(minutes=1))
            body = template_body(title=title, sport=sport, goal=goal)
            names[title] = self.publish(body).json()["template_id"]
        return names

    def suggestions(self, headers: Any = None, **params: Any) -> Any:
        return self.client.get(SUGGESTIONS, params=params, headers=headers or self.athlete)

    def test_personalized_suggestions_put_the_goal_first(self) -> None:
        """CA1: plantillas de sus deportes, primero las de su objetivo, con `reasons`."""
        self.seed()

        response = self.suggestions(sport=["running", "cycling"], primary_goal="COMPETITION")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        body = response.json()
        self.assertTrue(body["personalized"])
        self.assertEqual(
            [(item["title"], item["reasons"]) for item in body["items"]],
            [
                ("Competencia running", ["SPORT_MATCH", "GOAL_MATCH"]),
                ("Salud ciclismo", ["SPORT_MATCH"]),
                ("Salud running", ["SPORT_MATCH"]),
            ],
        )
        self.assertEqual(self.capabilities.calls, [(self.athlete_id, SUGGEST)])

    def test_without_goal_still_personalized_by_sport(self) -> None:
        """Decisión del 2026-10-24 ante CA2: sin objetivo, personalizada por deporte."""
        self.seed()

        body = self.suggestions(sport=["running"]).json()

        self.assertTrue(body["personalized"])
        self.assertEqual(
            [item["title"] for item in body["items"]], ["Competencia running", "Salud running"]
        )
        self.assertTrue(all(item["reasons"] == ["SPORT_MATCH"] for item in body["items"]))

    def test_without_sports_the_list_is_not_personalized_and_retired_is_hidden(self) -> None:
        """CA2 y CA3: sin deportes → no personalizada; una retirada no se sugiere."""
        names = self.seed()
        self.retire(names["Salud natación"])

        body = self.suggestions(limit="50").json()

        self.assertFalse(body["personalized"])
        self.assertEqual(
            [item["title"] for item in body["items"]],
            ["Salud ciclismo", "Competencia running", "Salud running"],
        )
        self.assertTrue(all(item["reasons"] == [] for item in body["items"]))

    def test_suggestions_require_athlete_capability_and_valid_filters(self) -> None:
        without = self.suggestions(headers=_bearer(uuid.uuid4(), "athlete"))
        provider = self.suggestions(headers=self.professional)
        self.assertEqual(
            (without.status_code, without.json()["code"]), (403, "capability_required")
        )
        self.assertEqual(
            (provider.status_code, provider.json()["code"]), (403, "permission_required")
        )
        calls = len(self.capabilities.calls)
        for field, params in (
            ("query.limit", {"limit": "0"}),
            ("query.sport", {"sport": ["running", "running"]}),
            ("query.primary_goal", {"primary_goal": "FUN"}),
        ):
            with self.subTest(field=field):
                response = self.suggestions(**params)
                self.assertEqual(response.status_code, 422)
                self.assertEqual(response.json()["details"][0]["field"], field)
        self.assertEqual(len(self.capabilities.calls), calls)
        self.capabilities.unavailable = True
        self.assertEqual(self.suggestions().status_code, 503)
        slow = self.client_with(_SlowCapabilities()).get(SUGGESTIONS, headers=self.athlete)
        self.assertEqual(slow.status_code, 504)


if __name__ == "__main__":
    unittest.main()
