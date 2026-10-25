"""Componente HU013 en web: consumo registrado por HU043 → outbox → proyección
`food_intake_view` → BFF web → Entrenamientos para el deportista y el profesional autorizado
(ADR-037).

Servicios reales (Usuarios, Afiliación, Entrenamientos y BFF web) con PostgreSQL migrado;
requiere `TEST_DATABASE_URL`. El consumo se registra con `POST /v1/me/food-intakes` del
servicio (la operación de HU043, que en producción llega por `bff-mobile` sin transformarla);
la publicación y la proyección de Entrenamientos se ejecutan con sus casos de uso, como en
`test_nutrition_plan_flow.py` (su transporte por Pub/Sub se prueba en la integración del
servicio).
"""

import json
import unittest
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
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
OWN = "/v1/me/food-intakes"


def utc(value: datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class FoodIntakeHistoryFlowTest(unittest.TestCase):
    def setUp(self) -> None:
        urls = {
            name: self.enterContext(temporary_database(f"hu013_flow_{name}"))
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
        self.training = self.enterContext(
            TestClient(
                create_training(
                    TrainingManagementSettings(
                        app_env="test",  # type: ignore[arg-type]
                        database_write_url=urls["training_w"],  # type: ignore[arg-type]
                        database_read_url=urls["training_r"],  # type: ignore[arg-type]
                        users_management_base_url="http://users-management",  # type: ignore[arg-type]
                        affiliation_management_base_url="http://affiliation-management",  # type: ignore[arg-type]
                        health_data_consent_version="health-test.v1",
                    ),
                    users_http=users,
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
                    training_http=self.training,
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
        self.organizer = self.register("organizer")

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

    def auth(self, user: dict[str, str]) -> dict[str, str]:
        return {"Authorization": f"Bearer {user['token']}"}

    def register_intake(self, consumed_at: datetime, meal_type: str) -> str:
        """HU043: el deportista registra el consumo con la operación real del servicio."""
        intake_id = str(uuid.uuid4())
        response = self.training.post(
            OWN,
            json={
                "intake_id": intake_id,
                "consumed_at": utc(consumed_at),
                "meal_type": meal_type,
                "items": [
                    {"food_code": "WHITE_RICE", "quantity": "150", "unit": "G"},
                    {"food_code": "WATER", "quantity": "500", "unit": "ML"},
                ],
            },
            headers={**self.auth(self.athlete), "Idempotency-Key": str(uuid.uuid4())},
        )
        self.assertEqual(response.status_code, 201, response.text)
        return intake_id

    def athlete_path(self) -> str:
        return f"/v1/athletes/{self.athlete['user_id']}/food-intakes"

    def test_hu013_vertical_backend_flow(self) -> None:
        """CA1, CA3, CA4 y CA5 por el canal web para ambos actores."""
        # CA5: sin consumos ni planes, colecciones vacías explícitas.
        empty = self.bff.get(OWN, headers=self.auth(self.athlete))
        plans = self.bff.get("/v1/me/nutrition-plans", headers=self.auth(self.athlete))
        self.assertEqual(empty.status_code, 200, empty.text)
        self.assertEqual(empty.json(), {"items": [], "next_cursor": None})
        self.assertEqual(plans.json()["items"], [])

        now = datetime.now(UTC).replace(microsecond=0)
        breakfast = self.register_intake(now - timedelta(days=1, hours=2), "BREAKFAST")
        lunch = self.register_intake(now - timedelta(hours=3), "LUNCH")
        hydration = self.register_intake(now - timedelta(hours=1), "HYDRATION")
        self.project_training_outbox()

        # CA1 y CA4: el deportista ve sus consumos descendentes y pagina por cursor.
        first = self.bff.get(OWN, headers=self.auth(self.athlete), params={"limit": 2})
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.headers["Cache-Control"], "no-store")
        self.assertEqual([i["intake_id"] for i in first.json()["items"]], [hydration, lunch])
        rest = self.bff.get(
            OWN,
            headers=self.auth(self.athlete),
            params={"limit": 2, "cursor": first.json()["next_cursor"]},
        )
        self.assertEqual([i["intake_id"] for i in rest.json()["items"]], [breakfast])
        self.assertIsNone(rest.json()["next_cursor"])
        item = first.json()["items"][1]
        self.assertEqual(item["meal_type"], "LUNCH")
        self.assertEqual(
            item["items"],
            [
                {"food_code": "WHITE_RICE", "quantity": "150.0", "unit": "G"},
                {"food_code": "WATER", "quantity": "500.0", "unit": "ML"},
            ],
        )
        lunch_only = self.bff.get(
            OWN, headers=self.auth(self.athlete), params={"meal_type": "LUNCH"}
        )
        self.assertEqual([i["intake_id"] for i in lunch_only.json()["items"]], [lunch])

        # CA3: sin autorización el profesional recibe `403` y el intento se audita.
        with self.assertLogs("sportapp.audit", level="INFO") as logs:
            denied = self.bff.get(self.athlete_path(), headers=self.auth(self.professional))
            granted = self.bff.post(
                "/v1/me/training-authorizations",
                json={"professional_email": self.professional["email"]},
                headers=self.auth(self.athlete),
            )
            self.assertEqual(granted.status_code, 201, granted.text)
            self.project_training_outbox()
            allowed = self.bff.get(self.athlete_path(), headers=self.auth(self.professional))
            foreign = self.bff.get(self.athlete_path(), headers=self.auth(self.stranger))

        self.assertEqual(
            (denied.status_code, denied.json()["code"]), (403, "athlete_authorization_required")
        )
        self.assertEqual(allowed.status_code, 200, allowed.text)
        self.assertEqual(
            [i["intake_id"] for i in allowed.json()["items"]], [hydration, lunch, breakfast]
        )
        self.assertEqual(
            (foreign.status_code, foreign.json()["code"]), (403, "athlete_authorization_required")
        )
        access = [
            record.__dict__["fields"]
            for record in logs.records
            if record.getMessage() == "audit.nutrition_history_access"
        ]
        self.assertEqual(
            [entry["result"] for entry in access],
            [
                "rejected:athlete_authorization_required",
                "granted",
                "rejected:athlete_authorization_required",
            ],
        )
        for entry in access:
            self.assertEqual(set(entry), {"result", "professional_id", "athlete_id"})

        # CA3: actores distintos se rechazan en el borde; la revocación corta el acceso.
        by_organizer = self.bff.get(self.athlete_path(), headers=self.auth(self.organizer))
        own_by_professional = self.bff.get(OWN, headers=self.auth(self.professional))
        self.assertEqual(by_organizer.json()["code"], "permission_required")
        self.assertEqual(own_by_professional.json()["code"], "permission_required")
        revoked = self.bff.delete(
            f"/v1/me/training-authorizations/{self.professional['user_id']}",
            headers=self.auth(self.athlete),
        )
        self.assertIn(revoked.status_code, (200, 204), revoked.text)
        self.project_training_outbox()
        after_revoke = self.bff.get(self.athlete_path(), headers=self.auth(self.professional))
        self.assertEqual(after_revoke.status_code, 403)

    def test_invalid_period_or_cursor_is_validation_error(self) -> None:
        """CA2 por el BFF: el `422` del servicio se entrega sin alterar."""
        cases = (
            ({"from": "2026-10-09", "to": "2026-10-08"}, "query.to", "to_before_from"),
            ({"cursor": "corrupto"}, "query.cursor", "invalid_cursor"),
        )
        for params, field, issue in cases:
            with self.subTest(field=field):
                response = self.bff.get(OWN, headers=self.auth(self.athlete), params=params)
                self.assertEqual(response.status_code, 422, response.text)
                self.assertEqual(response.json()["code"], "validation_error")
                self.assertEqual(response.json()["details"], [{"field": field, "issue": issue}])


if __name__ == "__main__":
    unittest.main()
