"""Componente HU014 en web: BFF → Usuarios (lookup y restricciones) → Entrenamientos → outbox →
proyección → consulta del deportista (ADR-037).

Servicios reales (Usuarios, Afiliación, Entrenamientos y BFF) con PostgreSQL migrado;
requiere `TEST_DATABASE_URL`. Como en `test_training_plan_flow.py`, el alta `FREE`, el
directorio de Usuarios y la publicación/proyección de Entrenamientos se ejecutan con sus casos
de uso (su transporte por Pub/Sub se prueba en la integración de cada servicio).

El transporte de Usuarios que usa Entrenamientos puede sustituir solo la respuesta de
`GET /v1/users/{id}/nutrition-restrictions` con datos sintéticos de ADR-035 (o simular su
caída); todas las demás llamadas llegan al servicio real. Desde HU009 el deportista registra
sus hábitos alimenticios: `test_registered_habits_block_the_plan_through_the_real_contract`
recorre el bloqueo sin sustituir nada.
"""

import json
import unittest
import uuid
from typing import Any

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from affiliation_management.application.commands.provision_free_subscription import (
    ProvisionFreeSubscription,
)
from affiliation_management.application.messages import RegisteredUser
from affiliation_management.infrastructure.clock import SystemClock as AffiliationClock
from affiliation_management.infrastructure.persistence.unit_of_work import (
    SqlAlchemyProvisioningUnitOfWork,
)
from affiliation_management.main import create_app as create_affiliation
from affiliation_management.settings import AffiliationManagementSettings
from bff_web.main import create_app as create_bff
from bff_web.settings import BffWebSettings
from sportapp_platform.migrations import upgrade
from sportapp_platform.testing.keys import ed25519_private_key_pem
from sportapp_platform.testing.postgres import temporary_database
from training_management.application.messages import parse_training_event
from training_management.application.projections.snapshot_projection import TrainingProjector
from training_management.infrastructure.clock import SystemClock
from training_management.infrastructure.persistence_read.plans import (
    SqlAlchemyProjectionUnitOfWork,
)
from training_management.infrastructure.persistence_write.models import OutboxEventRow
from training_management.infrastructure.persistence_write.outbox import SqlAlchemyOutboxStore
from training_management.main import create_app as create_training
from training_management.settings import OutboxPublisherSettings, TrainingManagementSettings
from training_management.workers.outbox_publisher import OutboxRelay
from users_management.application.messages import parse_users_event
from users_management.application.projections.users_projection import UsersProjector
from users_management.infrastructure.clock import SystemClock as UsersClock
from users_management.infrastructure.persistence_read.projection import (
    SqlAlchemyProjectionUnitOfWork as UsersProjectionUnitOfWork,
)
from users_management.infrastructure.persistence_write.outbox import (
    SqlAlchemyOutboxStore as UsersOutboxStore,
)
from users_management.main import create_app as create_users
from users_management.settings import UsersManagementSettings
from users_management.workers.outbox_publisher import build_envelope as users_envelope

USERS_WRITE = "users_management.infrastructure.persistence_write:migrations"
USERS_READ = "users_management.infrastructure.persistence_read:migrations"
AFFILIATION = "affiliation_management.infrastructure.persistence:migrations"
TRAINING_WRITE = "training_management.infrastructure.persistence_write:migrations"
TRAINING_READ = "training_management.infrastructure.persistence_read:migrations"
POLICIES = "2026-10"
CATALOG = "plans-2026-10-13.v1"
PASSWORD = "contraseña-muy-secreta"
ORIGIN = "http://localhost:4200"
RESTRICTIONS_SUFFIX = "/nutrition-restrictions"


def plan_request(athlete_email: str, *items: dict[str, str]) -> dict[str, Any]:
    return {
        "athlete_email": athlete_email,
        "title": "Recuperación de pretemporada",
        "objectives": ["Proteína después de entrenar"],
        "start_date": "2026-11-02",
        "end_date": "2026-11-29",
        "meals": [
            {
                "meal_type": "LUNCH",
                "items": list(items)
                or [{"food_code": "WHITE_RICE", "quantity": "150", "unit": "G"}],
            },
            {
                "meal_type": "HYDRATION",
                "items": [{"food_code": "WATER", "quantity": "500", "unit": "ML"}],
            },
        ],
    }


class UsersAsSeenByTraining(httpx.BaseTransport):
    """Usuarios real para Entrenamientos; solo la respuesta de restricciones se puede
    sustituir por hábitos sintéticos (`habits`) o simular caída (`down`)."""

    def __init__(self, users: TestClient) -> None:
        self.users = users
        self.habits: dict[str, Any] | None = None
        self.down = False
        self.restriction_reads = 0

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(RESTRICTIONS_SUFFIX):
            self.restriction_reads += 1
            if self.down:
                raise httpx.ConnectError("usuarios caído")
            if self.habits is not None:
                return httpx.Response(200, json=self.habits)
        answer = self.users.request(
            request.method,
            request.url.path,
            params=request.url.params,
            headers={k: v for k, v in request.headers.items() if k.lower() != "host"},
            content=request.content,
        )
        return httpx.Response(answer.status_code, headers=answer.headers, content=answer.content)


class NutritionPlanFlowTest(unittest.TestCase):
    def setUp(self) -> None:
        urls = {
            name: self.enterContext(temporary_database(f"hu014_flow_{name}"))
            for name in ("users_w", "users_r", "affiliation", "training_w", "training_r")
        }
        for name, history in (
            ("users_w", USERS_WRITE),
            ("users_r", USERS_READ),
            ("affiliation", AFFILIATION),
            ("training_w", TRAINING_WRITE),
            ("training_r", TRAINING_READ),
        ):
            upgrade(history, urls[name])
        self.urls = urls
        users = self.enterContext(
            TestClient(
                create_users(
                    UsersManagementSettings(
                        app_env="test",  # type: ignore[arg-type]
                        database_write_url=urls["users_w"],  # type: ignore[arg-type]
                        database_read_url=urls["users_r"],  # type: ignore[arg-type]
                        registration_policies_version=POLICIES,
                        health_data_consent_version="health-test.v1",
                        jwt_private_key=ed25519_private_key_pem(),  # type: ignore[arg-type]
                    )
                ),
                base_url="http://users-management",
            )
        )
        affiliation = self.enterContext(
            TestClient(
                create_affiliation(
                    AffiliationManagementSettings(
                        app_env="test",  # type: ignore[arg-type]
                        database_url=urls["affiliation"],  # type: ignore[arg-type]
                        affiliation_expected_catalog_version=CATALOG,
                        users_management_base_url="http://users-management",  # type: ignore[arg-type]
                    )
                ),
                base_url="http://affiliation-management",
            )
        )
        self.users_seen_by_training = UsersAsSeenByTraining(users)
        training_users = httpx.Client(
            transport=self.users_seen_by_training, base_url="http://users-management"
        )
        training = self.enterContext(
            TestClient(
                create_training(
                    TrainingManagementSettings(
                        app_env="test",  # type: ignore[arg-type]
                        database_write_url=urls["training_w"],  # type: ignore[arg-type]
                        database_read_url=urls["training_r"],  # type: ignore[arg-type]
                        users_management_base_url="http://users-management",  # type: ignore[arg-type]
                        affiliation_management_base_url="http://affiliation-management",  # type: ignore[arg-type]
                    ),
                    users_http=training_users,
                    affiliation_http=affiliation,
                ),
                base_url="http://training-management",
            )
        )
        self.bff = self.enterContext(
            TestClient(
                create_bff(
                    BffWebSettings(
                        app_env="test",  # type: ignore[arg-type]
                        users_management_base_url="http://users-management",  # type: ignore[arg-type]
                        affiliation_management_base_url="http://affiliation-management",  # type: ignore[arg-type]
                        training_management_base_url="http://training-management",  # type: ignore[arg-type]
                        complementary_services_base_url="http://services.invalid",  # type: ignore[arg-type]
                        events_management_base_url="http://events.invalid",  # type: ignore[arg-type]
                        cors_allowed_origins=(ORIGIN,),
                    ),
                    users_http=users,
                    affiliation_http=affiliation,
                    training_http=training,
                ),
                base_url="https://bff-web",
                headers={"Origin": ORIGIN},
            )
        )
        self.engines = {name: create_engine(url) for name, url in urls.items()}
        for engine in self.engines.values():
            self.addCleanup(engine.dispose)
        self.athlete = self.register("athlete")
        self.professional = self.register("service_provider")
        self.stranger = self.register("service_provider")

    def register(self, actor_type: str) -> dict[str, str]:
        email = f"{actor_type}-{uuid.uuid4().hex[:8]}@example.com"
        registered = self.bff.post(
            "/v1/users",
            json={
                "full_name": "Persona Sintética",
                "email": email,
                "password": PASSWORD,
                "actor_type": actor_type,
                "accepted_policies_version": POLICIES,
            },
            headers={"Idempotency-Key": str(uuid.uuid4())},
        )
        self.assertEqual(registered.status_code, 201, registered.text)
        user_id = registered.json()["user_id"]
        self.project_users_outbox()
        if actor_type == "athlete":
            ProvisionFreeSubscription(
                unit_of_work=lambda: SqlAlchemyProvisioningUnitOfWork(
                    sessionmaker(self.engines["affiliation"])
                ),
                clock=AffiliationClock(),
            )(
                RegisteredUser(
                    event_id=uuid.uuid4(),
                    user_id=uuid.UUID(user_id),
                    actor_type=actor_type,
                    correlation_id=None,
                )
            )
        login = self.bff.post("/v1/sessions", json={"email": email, "password": PASSWORD})
        self.assertEqual(login.status_code, 200, login.text)
        return {"user_id": user_id, "email": email, "token": login.json()["access_token"]}

    def project_users_outbox(self) -> None:
        """`usuarios-outbox-publisher` + `usuarios-read-projector` sin Pub/Sub."""
        outbox = UsersOutboxStore(sessionmaker(self.engines["users_w"]))
        project = UsersProjector(
            unit_of_work=lambda: UsersProjectionUnitOfWork(sessionmaker(self.engines["users_r"])),
            clock=UsersClock(),
        )
        for event in outbox.claim_batch("w-test", 10, 30):
            project(parse_users_event(json.loads(json.dumps(users_envelope(event)))))
            outbox.mark_published(event.event_id, "w-test")

    def project_training_outbox(self) -> list[dict[str, Any]]:
        """`entrenamientos-outbox-publisher` + `entrenamientos-read-projector` sin Pub/Sub."""
        envelopes: list[dict[str, Any]] = []

        class Capture:
            def publish(self, envelope: dict[str, Any], ordering_key: str) -> None:
                envelopes.append(json.loads(json.dumps(envelope)))

        OutboxRelay(
            SqlAlchemyOutboxStore(sessionmaker(self.engines["training_w"])),
            Capture(),
            OutboxPublisherSettings(
                app_env="test",  # type: ignore[arg-type]
                database_write_url=self.urls["training_w"],  # type: ignore[arg-type]
                pubsub_project_id="sportapp-test",
                worker_id="w-test",
            ),
        ).run_once()
        project = TrainingProjector(
            unit_of_work=lambda: SqlAlchemyProjectionUnitOfWork(
                sessionmaker(self.engines["training_r"])
            ),
            clock=SystemClock(),
        )
        for envelope in envelopes:
            project(parse_training_event(envelope))
        return envelopes

    def auth(self, user: dict[str, str], **extra: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {user['token']}", **extra}

    def create(self, body: dict[str, Any], key: str | None = None) -> httpx.Response:
        return self.bff.post(
            "/v1/nutrition-plans",
            json=body,
            headers=self.auth(self.professional, **{"Idempotency-Key": key or str(uuid.uuid4())}),
        )

    def authorize(self, professional: dict[str, str]) -> None:
        granted = self.bff.post(
            "/v1/me/training-authorizations",
            json={"professional_email": professional["email"]},
            headers=self.auth(self.athlete),
        )
        self.assertEqual(granted.status_code, 201, granted.text)

    def nutrition_events(self) -> int:
        with sessionmaker(self.engines["training_w"])() as session:
            return (
                session.scalar(
                    select(func.count())
                    .select_from(OutboxEventRow)
                    .where(OutboxEventRow.event_type == "plan.alimentacion_creado.v1")
                )
                or 0
            )

    def test_hu014_vertical_backend_flow(self) -> None:
        """CA1, CA2, CA3, CA5 y CA6 por el canal web de punta a punta en el backend."""
        email = self.athlete["email"]

        # CA3: sin autorización del deportista no hay plan, y Usuarios ni se consulta.
        unauthorized = self.create(plan_request(email))
        self.assertEqual(
            (unauthorized.status_code, unauthorized.json()["code"]),
            (403, "athlete_authorization_required"),
        )
        self.assertEqual(self.users_seen_by_training.restriction_reads, 0)
        self.authorize(self.professional)

        # CA2: alergia y restricción registradas bloquean el plan, por ítem.
        self.users_seen_by_training.habits = {
            "restrictions": ["GLUTEN_FREE"],
            "allergies": ["PEANUTS"],
            "diet_pattern": "VEGAN",
            "version": 3,
        }
        blocked = self.create(
            plan_request(
                email,
                {"food_code": "PEANUT_BUTTER", "quantity": "30", "unit": "G"},
                {"food_code": "CHICKEN_BREAST", "quantity": "150", "unit": "G"},
                {"food_code": "PASTA", "quantity": "100", "unit": "G"},
            )
        )
        self.assertEqual(
            (blocked.status_code, blocked.json()["code"]), (422, "nutrition_plan_invalid")
        )
        self.assertEqual(
            blocked.json()["details"],
            [
                {"field": "body.meals.0.items.0.food_code", "issue": "conflicts_with_allergy"},
                {"field": "body.meals.0.items.1.food_code", "issue": "conflicts_with_restriction"},
                {"field": "body.meals.0.items.2.food_code", "issue": "conflicts_with_restriction"},
            ],
        )
        self.assertEqual(self.nutrition_events(), 0, "un plan bloqueado no se crea")

        # CA6: Usuarios caído → 503 sin plan.
        self.users_seen_by_training.habits = None
        self.users_seen_by_training.down = True
        down = self.create(plan_request(email))
        self.assertEqual((down.status_code, down.json()["code"]), (503, "upstream_unavailable"))
        self.assertEqual(self.nutrition_events(), 0)

        # CA1 y CA6: Usuarios real (sin hábitos registrados) → plan con aviso; reintento igual.
        self.users_seen_by_training.down = False
        key = str(uuid.uuid4())
        created = self.create(plan_request(email), key)
        replayed = self.create(plan_request(email), key)
        self.assertEqual(created.status_code, 201, created.text)
        plan = created.json()
        self.assertEqual((plan["status"], plan["version"]), ("ASSIGNED", 1))
        self.assertEqual(plan["athlete_id"], self.athlete["user_id"])
        self.assertEqual(plan["warnings"], ["nutrition_habits_missing"])
        self.assertEqual(plan["meals"][0]["items"][0]["quantity"], "150.0")
        self.assertEqual(created.headers["Location"], f"/v1/nutrition-plans/{plan['plan_id']}")
        self.assertEqual((replayed.json(), replayed.headers["Idempotent-Replayed"]), (plan, "true"))

        # CA5: un único evento; el deportista lo ve una vez proyectado.
        plan_url = f"/v1/nutrition-plans/{plan['plan_id']}"
        self.assertEqual(self.bff.get(plan_url, headers=self.auth(self.athlete)).status_code, 404)
        envelopes = self.project_training_outbox()
        nutrition = [e for e in envelopes if e["event_type"] == "plan.alimentacion_creado.v1"]
        self.assertEqual(len(nutrition), 1)
        self.assertNotIn("warnings", nutrition[0]["payload"])
        mine = self.bff.get("/v1/me/nutrition-plans", headers=self.auth(self.athlete))
        self.assertEqual(mine.status_code, 200, mine.text)
        expected = {k: v for k, v in plan.items() if k != "warnings"}
        self.assertEqual(mine.json()["items"], [expected])
        self.assertEqual(mine.headers["Cache-Control"], "no-store")
        for user in (self.athlete, self.professional):
            seen = self.bff.get(plan_url, headers=self.auth(user))
            self.assertEqual((seen.status_code, seen.json()), (200, expected))

        # CA3: un tercero no lo ve; otro actor no consulta la lista.
        third = self.bff.get(plan_url, headers=self.auth(self.stranger))
        self.assertEqual(
            (third.status_code, third.json()["code"]), (404, "nutrition_plan_not_found")
        )
        organizer_list = self.bff.get("/v1/me/nutrition-plans", headers=self.auth(self.stranger))
        self.assertEqual(organizer_list.status_code, 403)

    def test_registered_habits_block_the_plan_through_the_real_contract(self) -> None:
        """HU009 CA3 y HU014 CA2: los hábitos que el deportista registra por el BFF se proyectan
        y Entrenamientos obtiene sus alergias y restricciones por el contrato interno real."""
        registered = self.bff.put(
            "/v1/me/nutrition-habits",
            json={
                "consent": {"version": "health-test.v1"},
                "diet_pattern": "VEGETARIAN",
                "restrictions": ["GLUTEN_FREE"],
                "allergies": ["PEANUTS"],
                "meals_per_day": 4,
            },
            headers=self.auth(self.athlete, **{"Idempotency-Key": str(uuid.uuid4())}),
        )
        self.assertEqual(registered.status_code, 201, registered.text)
        self.project_users_outbox()
        self.authorize(self.professional)

        blocked = self.create(
            plan_request(
                self.athlete["email"],
                {"food_code": "PEANUT_BUTTER", "quantity": "30", "unit": "G"},
                {"food_code": "CHICKEN_BREAST", "quantity": "150", "unit": "G"},
                {"food_code": "PASTA", "quantity": "100", "unit": "G"},
            )
        )
        allowed = self.create(plan_request(self.athlete["email"]))

        self.assertEqual(
            (blocked.status_code, blocked.json()["code"]), (422, "nutrition_plan_invalid")
        )
        self.assertEqual(
            blocked.json()["details"],
            [
                {"field": "body.meals.0.items.0.food_code", "issue": "conflicts_with_allergy"},
                {"field": "body.meals.0.items.1.food_code", "issue": "conflicts_with_restriction"},
                {"field": "body.meals.0.items.2.food_code", "issue": "conflicts_with_restriction"},
            ],
        )
        self.assertEqual(allowed.status_code, 201, allowed.text)
        self.assertEqual(allowed.json()["warnings"], [], "con hábitos registrados no hay aviso")
        self.assertEqual(self.users_seen_by_training.restriction_reads, 2)
        self.assertEqual(self.nutrition_events(), 1)

    def test_food_catalog_through_the_web_channel(self) -> None:
        catalog = self.bff.get("/v1/food-catalog", headers=self.auth(self.professional))
        cached = self.bff.get(
            "/v1/food-catalog",
            headers=self.auth(self.athlete, **{"If-None-Match": catalog.headers["ETag"]}),
        )

        self.assertEqual(catalog.status_code, 200, catalog.text)
        body = catalog.json()
        self.assertEqual(body["catalog_version"], "food-catalog-2026-10-13.v1")
        codes = {item["food_code"]: item for item in body["items"]}
        self.assertEqual(codes["PEANUT_BUTTER"]["allergens"], ["PEANUTS"])
        self.assertIn("VEGAN", codes["CHICKEN_BREAST"]["incompatible_with"])
        self.assertEqual((cached.status_code, cached.content), (304, b""))


if __name__ == "__main__":
    unittest.main()
