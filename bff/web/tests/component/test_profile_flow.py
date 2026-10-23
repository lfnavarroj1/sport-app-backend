"""Componente HU005 en web: BFF → Usuarios → outbox → Pub/Sub → proyector → consulta.

Servicios reales (Usuarios y BFF) con PostgreSQL de escritura y de lectura migrados,
el publicador `usuarios-outbox-publisher` y el manejador de `usuarios-read-projector`
contra el emulador de Pub/Sub. Requiere `TEST_DATABASE_URL` y `PUBSUB_EMULATOR_HOST`.
También comprueba que la suscripción del alta gratuita de Afiliación no recibe los
eventos de perfil (R-19, ADR-034).
"""

import contextlib
import json
import logging
import os
import time
import unittest
import uuid
from collections.abc import Callable
from typing import Any

from fastapi.testclient import TestClient
from google.cloud import pubsub_v1
from sqlalchemy import create_engine, pool
from sqlalchemy.orm import sessionmaker

from affiliation_management.settings import FREE_PLAN_SUBSCRIPTION
from affiliation_management.workers.pubsub_setup import (
    ensure_resources as ensure_affiliation_resources,
)
from bff_web.main import create_app as create_bff
from bff_web.settings import BffWebSettings
from sportapp_platform.migrations import upgrade
from sportapp_platform.testing.keys import ed25519_private_key_pem
from sportapp_platform.testing.postgres import temporary_database
from users_management.application.projections.users_projection import UsersProjector
from users_management.infrastructure.clock import SystemClock
from users_management.infrastructure.messaging.pubsub import PubSubEventPublisher
from users_management.infrastructure.persistence_read.projection import (
    SqlAlchemyProjectionUnitOfWork,
)
from users_management.infrastructure.persistence_write.outbox import SqlAlchemyOutboxStore
from users_management.main import create_app as create_users
from users_management.settings import (
    PROJECTOR_SUBSCRIPTION,
    USERS_TOPIC,
    OutboxPublisherSettings,
    UsersManagementSettings,
)
from users_management.workers.outbox_publisher import OutboxRelay
from users_management.workers.pubsub_setup import ensure_resources as ensure_users_resources
from users_management.workers.read_projector import ProjectionMessageHandler

USERS_WRITE = "users_management.infrastructure.persistence_write:migrations"
USERS_READ = "users_management.infrastructure.persistence_read:migrations"
POLICIES = "2026-10"
CONSENT = "health-test.v1"
PASSWORD = "contraseña-muy-secreta"
ORIGIN = "http://localhost:4200"
# Valores sintéticos que no deben aparecer en ningún log (CA4).
WEIGHT, TEMPERATURE, CITY = "68.5", "36.6", "Villavicencio"


def wait_until(condition: Callable[[], bool], timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.2)
    return False


def demographic(**overrides: Any) -> dict[str, Any]:
    return {
        "birth_date": "1992-04-18",
        "gender": "FEMALE",
        "country_code": "CO",
        "city": CITY,
        "marital_status": "SINGLE",
        "occupation": "Ingeniera",
        "income_range": "2_4_SMMLV",
        **overrides,
    }


def physiological(**overrides: Any) -> dict[str, Any]:
    return {
        "consent": {"version": CONSENT},
        "measured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 3600)),
        "weight_kg": WEIGHT,
        "height_cm": 172,
        "resting_heart_rate_bpm": 58,
        "blood_pressure": {"systolic_mmhg": 118, "diastolic_mmhg": 76},
        "body_temperature_c": TEMPERATURE,
        **overrides,
    }


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def text(self) -> str:
        return "\n".join(
            f"{record.getMessage()} {getattr(record, 'fields', '')} {record.args}"
            for record in self.records
        )


@unittest.skipUnless(os.environ.get("PUBSUB_EMULATOR_HOST"), "requiere el emulador de Pub/Sub")
class ProfileFlowCase(unittest.TestCase):
    """Usuarios y BFF reales, Pub/Sub del emulador; base de HU005 y HU006 (sin pruebas)."""

    def setUp(self) -> None:
        write_url = self.enterContext(temporary_database("bff_web_hu005_write"))
        read_url = self.enterContext(temporary_database("bff_web_hu005_read"))
        upgrade(USERS_WRITE, write_url)
        upgrade(USERS_READ, read_url)
        self.write_engine = create_engine(write_url, poolclass=pool.NullPool)
        self.read_engine = create_engine(read_url, poolclass=pool.NullPool)
        self.addCleanup(self.write_engine.dispose)
        self.addCleanup(self.read_engine.dispose)
        self.write_url = write_url

        self.capture = _Capture()
        root = logging.getLogger()
        previous_level = root.level
        root.addHandler(self.capture)
        root.setLevel(logging.DEBUG)
        self.addCleanup(root.setLevel, previous_level)
        self.addCleanup(root.removeHandler, self.capture)

        users = self.enterContext(
            TestClient(
                create_users(
                    UsersManagementSettings(
                        app_env="test",  # type: ignore[arg-type]
                        database_write_url=write_url,  # type: ignore[arg-type]
                        database_read_url=read_url,  # type: ignore[arg-type]
                        registration_policies_version=POLICIES,
                        health_data_consent_version=CONSENT,
                        jwt_private_key=ed25519_private_key_pem(),  # type: ignore[arg-type]
                    )
                ),
                base_url="http://users-management",
            )
        )
        settings = BffWebSettings(
            app_env="test",  # type: ignore[arg-type]
            users_management_base_url="http://users-management",  # type: ignore[arg-type]
            affiliation_management_base_url="http://affiliation.invalid",  # type: ignore[arg-type]
            training_management_base_url="http://training.invalid",  # type: ignore[arg-type]
            complementary_services_base_url="http://services.invalid",  # type: ignore[arg-type]
            events_management_base_url="http://events.invalid",  # type: ignore[arg-type]
            cors_allowed_origins=(ORIGIN,),
        )
        self.bff = self.enterContext(
            TestClient(
                create_bff(settings, users_http=users),
                base_url="https://bff-web",
                headers={"Origin": ORIGIN},
            )
        )

        self.project = f"sportapp-test-{uuid.uuid4().hex[:8]}"
        ensure_users_resources(self.project)
        ensure_affiliation_resources(self.project)
        self.subscriber = pubsub_v1.SubscriberClient()
        self.addCleanup(self.subscriber.close)

        self.athlete = self.session_for("athlete", "deportista.hu005@example.com")

    def session_for(self, actor_type: str, email: str) -> dict[str, str]:
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
        login = self.bff.post("/v1/sessions", json={"email": email, "password": PASSWORD})
        self.assertEqual(login.status_code, 200, login.text)
        return {
            "user_id": registered.json()["user_id"],
            "authorization": f"Bearer {login.json()['access_token']}",
        }

    def put(self, section: str, body: dict[str, Any], session: dict[str, str] | None = None) -> Any:
        session = session or self.athlete
        return self.bff.put(
            f"/v1/me/{section}-profile",
            json=body,
            headers={
                "Authorization": session["authorization"],
                "Idempotency-Key": str(uuid.uuid4()),
            },
        )

    def profile(self) -> dict[str, Any]:
        response = self.bff.get(
            "/v1/me/profile", headers={"Authorization": self.athlete["authorization"]}
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")
        document: dict[str, Any] = response.json()
        return document

    def publish_outbox(self) -> int:
        settings = OutboxPublisherSettings(
            app_env="test",  # type: ignore[arg-type]
            database_write_url=self.write_url,  # type: ignore[arg-type]
            pubsub_project_id=self.project,
            worker_id="w-hu005",
        )
        publisher = PubSubEventPublisher(self.project, USERS_TOPIC)
        try:
            return OutboxRelay(
                SqlAlchemyOutboxStore(sessionmaker(self.write_engine)), publisher, settings
            ).run_once()
        finally:
            publisher.close()

    def project_until(self, condition: Callable[[], bool]) -> bool:
        projector = UsersProjector(
            unit_of_work=lambda: SqlAlchemyProjectionUnitOfWork(sessionmaker(self.read_engine)),
            clock=SystemClock(),
        )
        streaming = self.subscriber.subscribe(
            self.subscriber.subscription_path(self.project, PROJECTOR_SUBSCRIPTION),
            callback=ProjectionMessageHandler(projector),
        )
        try:
            return wait_until(condition)
        finally:
            streaming.cancel()
            # Tras cancelar, `result` puede relanzar la cancelación; solo se espera el cierre.
            with contextlib.suppress(Exception):
                streaming.result(timeout=10)


class ProfileFlowTest(ProfileFlowCase):
    def test_athlete_registers_both_sections_and_reads_them_from_the_projection(self) -> None:
        """CA1, CA4 y CA7 de punta a punta; los consumidores de Afiliación no reciben `perfil.*`."""
        empty = self.profile()
        self.assertIsNone(empty["demographic"])
        self.assertIsNone(empty["physiological"])
        self.assertIsNone(empty["projected_at"])

        demo = self.put("demographic", demographic())
        physio = self.put("physiological", physiological())

        self.assertEqual(demo.status_code, 201, demo.text)
        self.assertEqual(physio.status_code, 201, physio.text)
        self.assertEqual(physio.json()["bmi"], "23.2")
        self.assertEqual(physio.json()["consent"]["version"], CONSENT)
        self.assertEqual(demo.json()["user_id"], self.athlete["user_id"])
        # Registro del deportista y las dos secciones.
        self.assertEqual(self.publish_outbox(), 3)

        projected = self.project_until(
            lambda: (
                self.profile()["versions"]
                == {
                    "demographic": 1,
                    "physiological": 1,
                    "sport_habits": None,
                }
            )
        )

        self.assertTrue(projected, "el proyector no aplicó las dos secciones")
        view = self.profile()
        expected_demo = {k: v for k, v in demo.json().items() if k != "user_id"}
        expected_physio = {k: v for k, v in physio.json().items() if k != "user_id"}
        self.assertEqual(view["user_id"], self.athlete["user_id"])
        self.assertEqual(view["demographic"], expected_demo)
        self.assertEqual(view["physiological"], expected_physio)
        self.assertIsNone(view["sport_habits"])
        self.assertIsNotNone(view["projected_at"])

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
        for value in (WEIGHT, TEMPERATURE, CITY, "23.2"):
            self.assertNotIn(value, logged)

    def test_replacement_requires_the_current_version(self) -> None:
        """CA6 por el BFF: versión vigente obligatoria; una versión vieja no sobrescribe."""
        created = self.put("demographic", demographic())
        replaced = self.put("demographic", demographic(expected_version=1, city="Bogotá"))
        stale = self.put("demographic", demographic(expected_version=1, city="Cali"))

        self.assertEqual(created.status_code, 201, created.text)
        self.assertEqual(replaced.status_code, 200, replaced.text)
        self.assertEqual(replaced.json()["version"], 2)
        self.assertEqual(stale.status_code, 409, stale.text)
        self.assertEqual(stale.json()["code"], "version_conflict")
        self.assertEqual(stale.json()["details"], [{"field": "expected_version", "issue": "2"}])

    def test_other_actors_are_rejected_and_athletes_are_isolated(self) -> None:
        """CA5: otro actor recibe `403`; cada deportista solo ve su perfil."""
        organizer = self.session_for("organizer", "organizador.hu005@example.com")
        other = self.session_for("athlete", "otra.deportista.hu005@example.com")
        self.assertEqual(self.put("demographic", demographic(), other).status_code, 201)

        forbidden = self.put("demographic", demographic(), organizer)
        own = self.profile()

        self.assertEqual(forbidden.status_code, 403, forbidden.text)
        self.assertEqual(forbidden.json()["code"], "permission_required")
        self.assertIsNone(own["demographic"])


if __name__ == "__main__":
    unittest.main()
