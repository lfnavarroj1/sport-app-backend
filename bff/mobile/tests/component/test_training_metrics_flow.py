"""Componente HU034 en móvil: BFF → Usuarios (sesión y JWKS) → Afiliación (capacidad
`training.metrics.ftp_vo2max`, incluida en el plan `FREE` del catálogo `plans-2026-10-13.v1`)
→ Entrenamientos → outbox → Pub/Sub → `entrenamientos-read-projector` → historial propio.

Servicios reales con PostgreSQL migrado (requiere `TEST_DATABASE_URL`). El plan `FREE` se
activa con el caso de uso real del alta gratuita (lo que ejecuta su consumidor, ADR-024). El
tramo de mensajería usa el emulador de Pub/Sub (`PUBSUB_EMULATOR_HOST`) con un proyecto propio.
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
from bff_mobile.main import create_app as create_bff
from bff_mobile.settings import BffMobileSettings
from sportapp_platform.migrations import upgrade
from sportapp_platform.testing.keys import ed25519_private_key_pem
from sportapp_platform.testing.postgres import temporary_database
from training_management.application.projections.snapshot_projection import TrainingProjector
from training_management.infrastructure.clock import SystemClock
from training_management.infrastructure.messaging.pubsub import PubSubEventPublisher
from training_management.infrastructure.persistence_read.plans import (
    SqlAlchemyProjectionUnitOfWork,
)
from training_management.infrastructure.persistence_write.models import OutboxEventRow
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
    "affiliation": "affiliation_management.infrastructure.persistence:migrations",
    "training_w": "training_management.infrastructure.persistence_write:migrations",
    "training_r": "training_management.infrastructure.persistence_read:migrations",
}
POLICIES = "2026-10"
CATALOG = "plans-2026-10-13.v1"
PASSWORD = "contraseña-muy-secreta"
CATALOG_PATH = "/v1/training-metrics/catalog"
CALCULATIONS = "/v1/training-metrics/calculations"


def utc_seconds(delta: timedelta, base: datetime | None = None) -> str:
    instant = (base or datetime.now(UTC)).replace(microsecond=0) + delta
    return instant.isoformat().replace("+00:00", "Z")


def wait_until(condition: Callable[[], bool], timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.2)
    return False


def ftp(mean_power_watts: int, measured_at: str) -> dict[str, Any]:
    return {
        "calculation_id": str(uuid.uuid4()),
        "metric_code": "FTP_ESTIMATED",
        "method_code": "FTP_20_MIN_95_V1",
        "method_version": 1,
        "measured_at": measured_at,
        "inputs": [{"code": "mean_power_watts", "value": mean_power_watts, "unit": "W"}],
    }


def cooper(distance_meters: int, measured_at: str) -> dict[str, Any]:
    return {
        "calculation_id": str(uuid.uuid4()),
        "metric_code": "VO2MAX_ESTIMATED",
        "method_code": "COOPER_12_MIN_V1",
        "method_version": 1,
        "measured_at": measured_at,
        "inputs": [{"code": "distance_meters", "value": distance_meters, "unit": "m"}],
    }


class TrainingMetricsFlowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.urls = {
            name: self.enterContext(temporary_database(f"hu034_{name}")) for name in HISTORIES
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
        affiliation = self.enterContext(
            TestClient(
                create_affiliation(
                    AffiliationManagementSettings(
                        app_env="test",  # type: ignore[arg-type]
                        database_url=self.urls["affiliation"],  # type: ignore[arg-type]
                        affiliation_expected_catalog_version=CATALOG,
                        users_management_base_url="http://users-management",  # type: ignore[arg-type]
                    )
                ),
                base_url="http://affiliation-management",
            )
        )
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
                    affiliation_http=affiliation,
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
                    ),
                    users_http=self.users,
                    affiliation_http=affiliation,
                    training_http=training,
                ),
                base_url="https://bff-mobile",
            )
        )
        self.engines = {name: create_engine(url) for name, url in self.urls.items()}
        for engine in self.engines.values():
            self.addCleanup(engine.dispose)

    def register(self, actor_type: str, *, free_plan: bool = True) -> dict[str, str]:
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
        user_id = registered.json()["user_id"]
        if actor_type == "athlete" and free_plan:
            # Alta gratuita real (ADR-024): el plan `FREE` incluye training.metrics.ftp_vo2max.
            ProvisionFreeSubscription(
                unit_of_work=lambda: SqlAlchemyProvisioningUnitOfWork(
                    sessionmaker(self.engines["affiliation"])
                ),
                clock=AffiliationClock(),
            )(RegisteredUser(uuid.uuid4(), uuid.UUID(user_id), actor_type, None))
        login = self.users.post("/v1/sessions", json={"email": email, "password": PASSWORD})
        self.assertEqual(login.status_code, 200, login.text)
        return {"user_id": user_id, "Authorization": f"Bearer {login.json()['access_token']}"}

    def catalog(self, user: dict[str, str], etag: str | None = None) -> httpx.Response:
        headers = {"Authorization": user["Authorization"]}
        if etag:
            headers["If-None-Match"] = etag
        return self.bff.get(CATALOG_PATH, headers=headers)

    def calculate(self, user: dict[str, str], body: dict[str, Any], key: str) -> httpx.Response:
        return self.bff.post(
            CALCULATIONS,
            json=body,
            headers={"Authorization": user["Authorization"], "Idempotency-Key": key},
        )

    def history(self, user: dict[str, str], query: str = "") -> httpx.Response:
        return self.bff.get(
            f"{CALCULATIONS}{query}", headers={"Authorization": user["Authorization"]}
        )

    def outbox_count(self) -> int:
        with sessionmaker(self.engines["training_w"])() as session:
            return session.scalar(select(func.count()).select_from(OutboxEventRow)) or 0

    def test_catalog_calculations_and_retries_through_the_mobile_channel(self) -> None:
        """CA1-CA5 y CA7 por el canal móvil: catálogo con caché, vectores e idempotencia."""
        athlete = self.register("athlete")

        catalog = self.catalog(athlete)
        self.assertEqual(catalog.status_code, 200, catalog.text)
        version = catalog.json()["catalog_version"]
        self.assertEqual(catalog.headers["ETag"], f'"{version}"')
        self.assertEqual(
            {(item["metric_code"], item["method_code"]) for item in catalog.json()["methods"]},
            {("FTP_ESTIMATED", "FTP_20_MIN_95_V1"), ("VO2MAX_ESTIMATED", "COOPER_12_MIN_V1")},
        )
        cached = self.catalog(athlete, etag=catalog.headers["ETag"])
        self.assertEqual((cached.status_code, cached.content), (304, b""))
        self.assertEqual(cached.headers["ETag"], catalog.headers["ETag"])

        body, key = ftp(250, utc_seconds(-timedelta(hours=1))), str(uuid.uuid4())
        created = self.calculate(athlete, body, key)
        self.assertEqual(created.status_code, 201, created.text)
        result = created.json()
        self.assertEqual(result["athlete_id"], athlete["user_id"])
        self.assertEqual(result["result"], {"value": 238, "unit": "W", "scale": 0})
        self.assertEqual(
            (result["status"], result["disclaimer_key"], result["version"]),
            ("CONFIRMED", "estimated_not_diagnostic", 1),
        )
        self.assertTrue(result["calculated_at"].endswith("Z"))
        self.assertEqual(result["inputs"], body["inputs"])

        replayed = self.calculate(athlete, body, key)
        self.assertEqual(
            (replayed.status_code, replayed.json(), replayed.headers["Idempotent-Replayed"]),
            (201, result, "true"),
        )
        same_calculation = self.calculate(athlete, body, str(uuid.uuid4()))
        self.assertEqual(
            (same_calculation.status_code, same_calculation.json()["result"]),
            (200, result["result"]),
        )
        reused_id = self.calculate(
            athlete, {**body, "inputs": [{**body["inputs"][0], "value": 300}]}, str(uuid.uuid4())
        )
        self.assertEqual(
            (reused_id.status_code, reused_id.json()["code"]), (409, "calculation_id_reused")
        )
        reused_key = self.calculate(athlete, ftp(260, body["measured_at"]), key)
        self.assertEqual(
            (reused_key.status_code, reused_key.json()["code"]), (409, "idempotency_key_reused")
        )

        vo2max = self.calculate(
            athlete, cooper(2400, utc_seconds(-timedelta(minutes=30))), str(uuid.uuid4())
        )
        self.assertEqual(vo2max.status_code, 201, vo2max.text)
        self.assertEqual(vo2max.json()["result"], {"value": 42.4, "unit": "mL/kg/min", "scale": 1})
        # Un evento por cálculo: los reintentos y el `calculation_id` repetido no publican otro.
        self.assertEqual(self.outbox_count(), 2)

    def test_actor_and_capability_are_enforced(self) -> None:
        """CA1: solo el deportista con training.metrics.ftp_vo2max; sin efectos al rechazar."""
        provider = self.register("service_provider")
        no_plan = self.register("athlete", free_plan=False)
        body = ftp(250, utc_seconds(-timedelta(hours=1)))

        for user, code in ((provider, "permission_required"), (no_plan, "capability_required")):
            for response in (
                self.catalog(user),
                self.calculate(user, body, str(uuid.uuid4())),
                self.history(user),
            ):
                with self.subTest(code=code, path=response.request.url.path):
                    self.assertEqual((response.status_code, response.json()["code"]), (403, code))
        self.assertEqual(self.outbox_count(), 0)

    @unittest.skipUnless(os.environ.get("PUBSUB_EMULATOR_HOST"), "requiere el emulador de Pub/Sub")
    def test_results_reach_the_own_history_through_pubsub(self) -> None:
        """CA5 y CA7: proyección propia ordenada por measured_at desc, sin resultados ajenos."""
        project_id = f"sportapp-test-{uuid.uuid4().hex[:8]}"
        ensure_resources(project_id)
        athlete, other = self.register("athlete"), self.register("athlete")
        older = self.calculate(
            athlete, ftp(250, utc_seconds(-timedelta(days=2))), str(uuid.uuid4())
        ).json()
        newer = self.calculate(
            athlete, cooper(2400, utc_seconds(-timedelta(days=1))), str(uuid.uuid4())
        ).json()
        foreign = self.calculate(
            other, ftp(300, utc_seconds(-timedelta(hours=3))), str(uuid.uuid4())
        ).json()

        self.publish(project_id)
        self.assertTrue(
            self.consume(
                project_id,
                lambda: (
                    len(self.history(athlete).json()["items"]) == 2
                    and len(self.history(other).json()["items"]) == 1
                ),
            )
        )

        own = self.history(athlete).json()
        self.assertEqual(
            [item["calculation_id"] for item in own["items"]],
            [newer["calculation_id"], older["calculation_id"]],
        )
        self.assertEqual(
            [item["result"] for item in own["items"]],
            [
                {"value": 42.4, "unit": "mL/kg/min", "scale": 1},
                {"value": 238, "unit": "W", "scale": 0},
            ],
        )
        self.assertIsNone(own["next_cursor"])
        self.assertNotIn(
            foreign["calculation_id"], [item["calculation_id"] for item in own["items"]]
        )
        self.assertEqual(
            [item["calculation_id"] for item in self.history(other).json()["items"]],
            [foreign["calculation_id"]],
        )

        only_ftp = self.history(athlete, "?metric_code=FTP_ESTIMATED").json()["items"]
        self.assertEqual([item["calculation_id"] for item in only_ftp], [older["calculation_id"]])
        first = self.history(athlete, "?limit=1").json()
        second = self.history(athlete, f"?limit=1&cursor={first['next_cursor']}").json()
        self.assertEqual(
            [first["items"][0]["calculation_id"], second["items"][0]["calculation_id"]],
            [newer["calculation_id"], older["calculation_id"]],
        )
        unknown = self.history(athlete, "?metric_code=LACTATE_THRESHOLD")
        self.assertEqual((unknown.status_code, unknown.json()["code"]), (422, "validation_error"))
        self.assertEqual(
            [detail["issue"] for detail in unknown.json()["details"]], ["unknown_metric"]
        )

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
