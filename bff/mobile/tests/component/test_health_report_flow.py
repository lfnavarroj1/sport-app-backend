"""Componente HU042 en móvil: BFF → Usuarios (sesión y JWKS) → Entrenamientos → outbox →
Pub/Sub → `entrenamientos-read-projector` → `health_report_view` (ADR-037, CA1-CA4).

Servicios reales con PostgreSQL migrado (requiere `TEST_DATABASE_URL`). «Mi salud» es una
opción base: el reporte no consulta capacidades; el plan `FREE` (alta gratuita real) solo se
usa para iniciar la sesión propia de HU032 que el reporte referencia. El tramo de mensajería
usa el emulador de Pub/Sub (`PUBSUB_EMULATOR_HOST`) con un proyecto propio.
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
from sqlalchemy import create_engine, func, select, update
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
from training_management.infrastructure.persistence_read.models import HealthReportViewRow
from training_management.infrastructure.persistence_read.plans import (
    SqlAlchemyProjectionUnitOfWork,
)
from training_management.infrastructure.persistence_write.models import (
    HealthReportRow,
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
    "affiliation": "affiliation_management.infrastructure.persistence:migrations",
    "training_w": "training_management.infrastructure.persistence_write:migrations",
    "training_r": "training_management.infrastructure.persistence_read:migrations",
}
POLICIES = "2026-10"
CATALOG = "plans-2026-10-13.v1"
PASSWORD = "contraseña-muy-secreta"
CONSENT = "local-dev"
REPORTS = "/v1/me/health-reports"


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


def health_report(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "report_id": str(uuid.uuid4()),
        "type": "INJURY",
        "body_area": "ANKLE_FOOT",
        "severity": "HIGH",
        "occurred_at": utc_seconds(-timedelta(hours=2)),
        "expected_recovery_days": 14,
        "consent": {"version": CONSENT},
    }
    values.update(overrides)
    return values


class HealthReportFlowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.urls = {
            name: self.enterContext(temporary_database(f"hu042_{name}")) for name in HISTORIES
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
                        health_data_consent_version="local-dev",
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
                        complementary_services_base_url="http://services.invalid",  # type: ignore[arg-type]
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
            # Alta gratuita real (ADR-024): el plan `FREE` permite iniciar sesiones (HU032).
            ProvisionFreeSubscription(
                unit_of_work=lambda: SqlAlchemyProvisioningUnitOfWork(
                    sessionmaker(self.engines["affiliation"])
                ),
                clock=AffiliationClock(),
            )(RegisteredUser(uuid.uuid4(), uuid.UUID(user_id), actor_type, None))
        login = self.users.post("/v1/sessions", json={"email": email, "password": PASSWORD})
        self.assertEqual(login.status_code, 200, login.text)
        return {"user_id": user_id, "Authorization": f"Bearer {login.json()['access_token']}"}

    def report(self, user: dict[str, str], body: dict[str, Any], key: str) -> httpx.Response:
        return self.bff.post(
            REPORTS,
            json=body,
            headers={"Authorization": user["Authorization"], "Idempotency-Key": key},
        )

    def start_session(self, user: dict[str, str]) -> str:
        session_id = str(uuid.uuid4())
        started = self.bff.post(
            "/v1/training-sessions",
            json={
                "session_id": session_id,
                "sport": "running",
                "started_at": utc_seconds(-timedelta(minutes=30)),
            },
            headers={"Authorization": user["Authorization"], "Idempotency-Key": str(uuid.uuid4())},
        )
        self.assertEqual(started.status_code, 201, started.text)
        return session_id

    def count(self, model: Any, engine: str = "training_w") -> int:
        with sessionmaker(self.engines[engine])() as session:
            return session.scalar(select(func.count()).select_from(model)) or 0

    def test_report_and_retries_through_the_mobile_channel(self) -> None:
        """CA1-CA3 por el canal móvil: reporte `OPEN`, sesión propia e idempotencia."""
        athlete = self.register("athlete")
        session_id = self.start_session(athlete)
        body, key = health_report(session_id=session_id), str(uuid.uuid4())

        created = self.report(athlete, body, key)
        self.assertEqual(created.status_code, 201, created.text)
        report = created.json()
        self.assertEqual(report["athlete_id"], athlete["user_id"])
        self.assertEqual((report["status"], report["version"]), ("OPEN", 1))
        self.assertEqual(report["session_id"], session_id)
        self.assertTrue(report["reported_at"].endswith("Z"))
        outbox_before = self.count(OutboxEventRow)

        replayed = self.report(athlete, body, key)
        self.assertEqual(
            (replayed.status_code, replayed.json(), replayed.headers["Idempotent-Replayed"]),
            (201, report, "true"),
        )
        existing = self.report(athlete, body, str(uuid.uuid4()))
        self.assertEqual((existing.status_code, existing.json()), (201, report))
        reused_id = self.report(athlete, {**body, "severity": "LOW"}, str(uuid.uuid4()))
        self.assertEqual(
            (reused_id.status_code, reused_id.json()["code"]), (409, "report_id_reused")
        )
        reused_key = self.report(athlete, health_report(), key)
        self.assertEqual(
            (reused_key.status_code, reused_key.json()["code"]), (409, "idempotency_key_reused")
        )
        # Un evento por reporte: los reintentos y el `report_id` repetido no publican otro.
        self.assertEqual(self.count(OutboxEventRow), outbox_before)
        self.assertEqual(self.count(HealthReportRow), 1)

    def test_rejections_persist_nothing(self) -> None:
        """CA2 y seguridad: otro actor `403`, sesión ajena `404`, datos inválidos `422`."""
        athlete, other = self.register("athlete"), self.register("athlete")
        provider = self.register("service_provider")
        foreign_session = self.start_session(other)

        forbidden = self.report(provider, health_report(), str(uuid.uuid4()))
        foreign = self.report(athlete, health_report(session_id=foreign_session), str(uuid.uuid4()))
        invalid = self.report(
            athlete,
            health_report(athlete_id=other["user_id"], consent={"version": "otra"}),
            str(uuid.uuid4()),
        )

        self.assertEqual(
            (forbidden.status_code, forbidden.json()["code"]), (403, "permission_required")
        )
        self.assertEqual(
            (foreign.status_code, foreign.json()["code"]), (404, "training_session_not_found")
        )
        self.assertEqual(invalid.status_code, 422)
        self.assertEqual(
            {(item["field"], item["issue"]) for item in invalid.json()["details"]},
            {
                ("athlete_id", "field_not_allowed"),
                ("consent.version", "unsupported_consent_version"),
            },
        )
        self.assertEqual(self.count(HealthReportRow), 0)

    @unittest.skipUnless(os.environ.get("PUBSUB_EMULATOR_HOST"), "requiere el emulador de Pub/Sub")
    def test_reports_reach_health_report_view_once_through_pubsub(self) -> None:
        """CA4: el evento llega por Pub/Sub y la vista lo incorpora una sola vez aunque se
        vuelva a publicar."""
        project_id = f"sportapp-test-{uuid.uuid4().hex[:8]}"
        ensure_resources(project_id)
        athlete = self.register("athlete", free_plan=False)
        first = self.report(athlete, health_report(), str(uuid.uuid4())).json()
        second = self.report(
            athlete, health_report(severity="LOW", type="DISCOMFORT"), str(uuid.uuid4())
        ).json()

        self.publish(project_id)
        self.assertTrue(
            self.consume(project_id, lambda: self.count(HealthReportViewRow, "training_r") == 2)
        )
        # Se vuelven a publicar los mismos eventos (entrega repetida): la vista no cambia.
        with sessionmaker(self.engines["training_w"])() as session, session.begin():
            session.execute(update(OutboxEventRow).values(published_at=None))
        self.publish(project_id)
        self.consume(project_id, lambda: False, timeout=5)

        with sessionmaker(self.engines["training_r"])() as session:
            rows = list(session.scalars(select(HealthReportViewRow)))
        self.assertEqual(
            {(str(row.report_id), row.severity, row.status, row.version) for row in rows},
            {(first["report_id"], "HIGH", "OPEN", 1), (second["report_id"], "LOW", "OPEN", 1)},
        )
        self.assertEqual({str(row.athlete_id) for row in rows}, {athlete["user_id"]})

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

    def consume(self, project_id: str, until: Callable[[], bool], timeout: float = 30.0) -> bool:
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
            return wait_until(until, timeout)
        finally:
            streaming.cancel()
            with contextlib.suppress(Exception):
                streaming.result(timeout=10)
            subscriber.close()


if __name__ == "__main__":
    unittest.main()
