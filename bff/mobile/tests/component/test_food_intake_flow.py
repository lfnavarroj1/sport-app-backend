"""Componente HU043 en móvil: BFF → Usuarios (sesión y JWKS) → Entrenamientos (catálogo
`food-catalog-2026-10-13.v1` precargado por migración) → outbox → Pub/Sub →
`entrenamientos-read-projector` → `food_intake_view` (la consultará HU013).

Servicios reales con PostgreSQL migrado (requiere `TEST_DATABASE_URL`). «Mi alimentación» es
una opción base: no consulta Afiliación. El tramo de mensajería usa el emulador de Pub/Sub
(`PUBSUB_EMULATOR_HOST`) con un proyecto propio.
"""

import contextlib
import os
import time
import unittest
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from fastapi.testclient import TestClient
from google.cloud import pubsub_v1
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from bff_mobile.main import create_app as create_bff
from bff_mobile.settings import BffMobileSettings
from sportapp_platform.migrations import upgrade
from sportapp_platform.testing.keys import ed25519_private_key_pem
from sportapp_platform.testing.postgres import temporary_database
from training_management.application.projections.snapshot_projection import TrainingProjector
from training_management.infrastructure.clock import SystemClock
from training_management.infrastructure.messaging.pubsub import PubSubEventPublisher
from training_management.infrastructure.persistence_read.models import FoodIntakeViewRow
from training_management.infrastructure.persistence_read.plans import (
    SqlAlchemyProjectionUnitOfWork,
)
from training_management.infrastructure.persistence_write.models import (
    FoodIntakeRow,
    OutboxEventRow,
)
from training_management.infrastructure.persistence_write.outbox import SqlAlchemyOutboxStore
from training_management.main import create_app as create_training
from training_management.settings import (
    PROJECTOR_SUBSCRIPTION,
    TRAINING_TOPIC,
    OutboxPublisherSettings,
    TrainingManagementSettings,
)
from training_management.workers.outbox_publisher import OutboxRelay
from training_management.workers.pubsub_setup import ensure_resources
from training_management.workers.read_projector import ProjectionMessageHandler
from users_management.main import create_app as create_users
from users_management.settings import UsersManagementSettings

HISTORIES = {
    "users_w": "users_management.infrastructure.persistence_write:migrations",
    "users_r": "users_management.infrastructure.persistence_read:migrations",
    "training_w": "training_management.infrastructure.persistence_write:migrations",
    "training_r": "training_management.infrastructure.persistence_read:migrations",
}
POLICIES = "2026-10"
PASSWORD = "contraseña-muy-secreta"
CATALOG_PATH = "/v1/food-catalog"
INTAKES = "/v1/me/food-intakes"
CATALOG_ETAG = '"food-catalog-2026-10-13.v1"'


def utc_seconds(delta: timedelta) -> str:
    instant = datetime.now(UTC).replace(microsecond=0) + delta
    return instant.isoformat().replace("+00:00", "Z")


def wait_until(condition: Callable[[], bool], timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.2)
    return False


def intake(consumed_at: str, meal_type: str = "BREAKFAST") -> dict[str, Any]:
    return {
        "intake_id": str(uuid.uuid4()),
        "consumed_at": consumed_at,
        "meal_type": meal_type,
        "items": [
            {"food_code": "OATS", "quantity": "80", "unit": "G"},
            {"food_code": "BANANA", "quantity": "1", "unit": "UNIT"},
            {"food_code": "MILK", "quantity": "250.0", "unit": "ML"},
        ],
    }


class FoodIntakeFlowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.urls = {
            name: self.enterContext(temporary_database(f"hu043_{name}")) for name in HISTORIES
        }
        for name, history in HISTORIES.items():
            upgrade(history, self.urls[name])
        self.users = self.enterContext(
            TestClient(
                create_users(
                    UsersManagementSettings(
                        app_env="test",  # type: ignore[arg-type]
                        database_write_url=self.urls["users_w"],  # type: ignore[arg-type]
                        database_read_url=self.urls["users_r"],  # type: ignore[arg-type]
                        registration_policies_version=POLICIES,
                        health_data_consent_version="health-test.v1",
                        jwt_private_key=ed25519_private_key_pem(),  # type: ignore[arg-type]
                    )
                ),
                base_url="http://users-management",
            )
        )
        # Ninguna operación de HU043 llama a Afiliación: un transporte que falla lo demuestra.
        self.affiliation_calls: list[str] = []

        def affiliation(request: httpx.Request) -> httpx.Response:
            self.affiliation_calls.append(request.url.path)
            return httpx.Response(500)

        affiliation_http = httpx.Client(
            base_url="http://affiliation-management", transport=httpx.MockTransport(affiliation)
        )
        self.addCleanup(affiliation_http.close)
        training = self.enterContext(
            TestClient(
                create_training(
                    TrainingManagementSettings(
                        app_env="test",  # type: ignore[arg-type]
                        database_write_url=self.urls["training_w"],  # type: ignore[arg-type]
                        database_read_url=self.urls["training_r"],  # type: ignore[arg-type]
                        users_management_base_url="http://users-management",  # type: ignore[arg-type]
                        affiliation_management_base_url="http://affiliation-management",  # type: ignore[arg-type]
                    ),
                    users_http=self.users,
                    affiliation_http=affiliation_http,
                ),
                base_url="http://training-management",
            )
        )
        self.bff = self.enterContext(
            TestClient(
                create_bff(
                    BffMobileSettings(
                        app_env="test",  # type: ignore[arg-type]
                        users_management_base_url="http://users-management",  # type: ignore[arg-type]
                        affiliation_management_base_url="http://affiliation-management",  # type: ignore[arg-type]
                        training_management_base_url="http://training-management",  # type: ignore[arg-type]
                        events_management_base_url="http://events.invalid",  # type: ignore[arg-type]
                        complementary_services_base_url="http://services.invalid",  # type: ignore[arg-type]
                    ),
                    users_http=self.users,
                    affiliation_http=affiliation_http,
                    training_http=training,
                ),
                base_url="https://bff-mobile",
            )
        )
        self.engines = {name: create_engine(url) for name, url in self.urls.items()}
        for engine in self.engines.values():
            self.addCleanup(engine.dispose)

    def register(self, actor_type: str) -> dict[str, str]:
        email = f"{actor_type}-{uuid.uuid4().hex[:8]}@example.com"
        registered = self.users.post(
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
        login = self.users.post("/v1/sessions", json={"email": email, "password": PASSWORD})
        self.assertEqual(login.status_code, 200, login.text)
        return {
            "user_id": registered.json()["user_id"],
            "Authorization": f"Bearer {login.json()['access_token']}",
        }

    def catalog(self, user: dict[str, str], etag: str | None = None) -> httpx.Response:
        headers = {"Authorization": user["Authorization"]}
        if etag:
            headers["If-None-Match"] = etag
        return self.bff.get(CATALOG_PATH, headers=headers)

    def submit(self, user: dict[str, str], body: dict[str, Any], key: str) -> httpx.Response:
        return self.bff.post(
            INTAKES,
            json=body,
            headers={"Authorization": user["Authorization"], "Idempotency-Key": key},
        )

    def count(self, model: Any, database: str) -> int:
        with sessionmaker(self.engines[database])() as session:
            return session.scalar(select(func.count()).select_from(model)) or 0

    def test_catalog_and_intakes_with_retries_through_the_mobile_channel(self) -> None:
        """CA1-CA3 y CA5/CA6 (caché): catálogo con `ETag`, registro normalizado e idempotencia."""
        athlete = self.register("athlete")

        catalog = self.catalog(athlete)
        self.assertEqual(catalog.status_code, 200, catalog.text)
        self.assertEqual(catalog.headers["ETag"], CATALOG_ETAG)
        self.assertEqual(len(catalog.json()["items"]), 41)
        oats = next(item for item in catalog.json()["items"] if item["food_code"] == "OATS")
        self.assertEqual(oats["allowed_units"], ["G", "CUP", "TBSP"])
        cached = self.catalog(athlete, etag=CATALOG_ETAG)
        self.assertEqual((cached.status_code, cached.content), (304, b""))

        body, key = intake(utc_seconds(-timedelta(hours=1))), str(uuid.uuid4())
        created = self.submit(athlete, body, key)
        self.assertEqual(created.status_code, 201, created.text)
        registered = created.json()
        self.assertEqual((registered["athlete_id"], registered["version"]), (athlete["user_id"], 1))
        self.assertEqual(
            [item["quantity"] for item in registered["items"]], ["80.0", "1.0", "250.0"]
        )
        self.assertTrue(registered["registered_at"].endswith("Z"))

        replayed = self.submit(athlete, body, key)
        self.assertEqual(
            (replayed.status_code, replayed.json(), replayed.headers["Idempotent-Replayed"]),
            (201, registered, "true"),
        )
        existing = self.submit(athlete, body, str(uuid.uuid4()))
        self.assertEqual((existing.status_code, existing.json()), (201, registered))
        reused_id = self.submit(athlete, {**body, "meal_type": "SNACK"}, str(uuid.uuid4()))
        self.assertEqual(
            (reused_id.status_code, reused_id.json()["code"]), (409, "intake_id_reused")
        )
        reused_key = self.submit(athlete, intake(body["consumed_at"]), key)
        self.assertEqual(
            (reused_key.status_code, reused_key.json()["code"]), (409, "idempotency_key_reused")
        )
        invalid = self.submit(
            athlete,
            {
                **intake(body["consumed_at"]),
                "items": [{"food_code": "PIZZA", "quantity": "1", "unit": "UNIT"}],
            },
            str(uuid.uuid4()),
        )
        self.assertEqual(
            (invalid.status_code, invalid.json()["details"]),
            (422, [{"field": "items[0].food_code", "issue": "unknown_value"}]),
        )
        # Un consumo y un evento: reintentos, `intake_id` repetido y rechazos no publican otro.
        self.assertEqual(
            (self.count(FoodIntakeRow, "training_w"), self.count(OutboxEventRow, "training_w")),
            (1, 1),
        )
        self.assertEqual(self.affiliation_calls, [])

    def test_only_athletes_and_identity_from_the_token(self) -> None:
        """Seguridad: otro actor → `403` en BFF; `athlete_id` en el cuerpo → `422`."""
        provider = self.register("service_provider")
        athlete = self.register("athlete")
        body = intake(utc_seconds(-timedelta(hours=1)))

        for response in (self.catalog(provider), self.submit(provider, body, str(uuid.uuid4()))):
            with self.subTest(path=response.request.url.path):
                self.assertEqual(
                    (response.status_code, response.json()["code"]), (403, "permission_required")
                )
        forged = self.submit(
            athlete, {**body, "athlete_id": provider["user_id"]}, str(uuid.uuid4())
        )
        self.assertEqual(
            (forged.status_code, forged.json()["details"]),
            (422, [{"field": "athlete_id", "issue": "field_not_allowed"}]),
        )
        self.assertEqual(self.count(OutboxEventRow, "training_w"), 0)

    @unittest.skipUnless(os.environ.get("PUBSUB_EMULATOR_HOST"), "requiere el emulador de Pub/Sub")
    def test_intakes_reach_food_intake_view_once_through_pubsub(self) -> None:
        """CA4: `consumo_alimento.registrado.v1` llega por Pub/Sub y la vista lo incorpora una
        vez, aunque el publicador vuelva a enviarlo."""
        project_id = f"sportapp-test-{uuid.uuid4().hex[:8]}"
        ensure_resources(project_id)
        athlete = self.register("athlete")
        first = self.submit(athlete, intake(utc_seconds(-timedelta(hours=2))), str(uuid.uuid4()))
        second = self.submit(
            athlete, intake(utc_seconds(-timedelta(hours=1)), "HYDRATION"), str(uuid.uuid4())
        )
        self.assertEqual((first.status_code, second.status_code), (201, 201))

        self.publish(project_id)
        # Reentrega: se vuelve a marcar el outbox como pendiente y se publica de nuevo.
        with sessionmaker(self.engines["training_w"])() as session, session.begin():
            session.execute(
                OutboxEventRow.__table__.update().values(
                    published_at=None, lease_owner=None, lease_until=None
                )
            )
        self.publish(project_id)
        self.assertTrue(
            self.consume(project_id, lambda: self.count(FoodIntakeViewRow, "training_r") == 2)
        )
        time.sleep(1)

        with sessionmaker(self.engines["training_r"])() as session:
            rows = list(
                session.scalars(
                    select(FoodIntakeViewRow).order_by(
                        FoodIntakeViewRow.consumed_at.desc(), FoodIntakeViewRow.intake_id.desc()
                    )
                )
            )
        self.assertEqual(
            [str(row.intake_id) for row in rows],
            [second.json()["intake_id"], first.json()["intake_id"]],
        )
        self.assertEqual({str(row.athlete_id) for row in rows}, {athlete["user_id"]})
        self.assertEqual(rows[1].items, first.json()["items"])
        self.assertEqual((rows[0].meal_type, rows[0].version), ("HYDRATION", 1))

    def publish(self, project_id: str) -> None:
        publisher = PubSubEventPublisher(project_id, TRAINING_TOPIC)
        try:
            OutboxRelay(
                SqlAlchemyOutboxStore(sessionmaker(self.engines["training_w"])),
                publisher,
                OutboxPublisherSettings(
                    app_env="test",  # type: ignore[arg-type]
                    database_write_url=self.urls["training_w"],  # type: ignore[arg-type]
                    pubsub_project_id=project_id,
                    worker_id="w-test",
                ),
            ).run_once()
        finally:
            publisher.close()

    def consume(self, project_id: str, until: Callable[[], bool]) -> bool:
        subscriber = pubsub_v1.SubscriberClient()
        project = TrainingProjector(
            unit_of_work=lambda: SqlAlchemyProjectionUnitOfWork(
                sessionmaker(self.engines["training_r"])
            ),
            clock=SystemClock(),
        )
        streaming = subscriber.subscribe(
            subscriber.subscription_path(project_id, PROJECTOR_SUBSCRIPTION),
            callback=ProjectionMessageHandler(project),
        )
        try:
            return wait_until(until)
        finally:
            streaming.cancel()
            with contextlib.suppress(Exception):
                streaming.result(timeout=10)
            subscriber.close()


if __name__ == "__main__":
    unittest.main()
