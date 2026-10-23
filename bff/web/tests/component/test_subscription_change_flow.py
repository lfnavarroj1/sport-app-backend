"""Componente HU016 (ADR-036): `bff-web` → Usuarios (sesión y JWKS) → Afiliación → PostgreSQL →
outbox → `afiliacion-outbox-publisher` → emulador de Pub/Sub, y capacidades del plan nuevo.

Servicios reales con PostgreSQL migrado (requiere `TEST_DATABASE_URL`); el recorrido por
Pub/Sub requiere además `PUBSUB_EMULATOR_HOST`. El alta gratuita se dispara con el caso de
uso de Afiliación sobre el mismo evento que publicaría Usuarios (HU001 cubre ese tramo).
"""

import json
import os
import time
import unittest
import uuid
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
from fastapi.testclient import TestClient
from google.cloud import pubsub_v1
from jsonschema import Draft202012Validator, FormatChecker
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from affiliation_management.application.commands.provision_free_subscription import (
    ProvisionFreeSubscription,
)
from affiliation_management.application.messages import RegisteredUser
from affiliation_management.infrastructure.clock import SystemClock
from affiliation_management.infrastructure.messaging.pubsub import PubSubEventPublisher
from affiliation_management.infrastructure.persistence.models import (
    BillingCommitmentRow,
    OutboxEventRow,
    SubscriptionRow,
)
from affiliation_management.infrastructure.persistence.outbox import SqlAlchemyOutboxStore
from affiliation_management.infrastructure.persistence.unit_of_work import (
    SqlAlchemyProvisioningUnitOfWork,
)
from affiliation_management.main import create_app as create_affiliation
from affiliation_management.settings import (
    AFFILIATION_TOPIC,
    AffiliationManagementSettings,
    OutboxPublisherSettings,
)
from affiliation_management.workers.outbox_publisher import OutboxRelay
from affiliation_management.workers.pubsub_setup import ensure_resources
from bff_web.main import create_app as create_bff
from bff_web.settings import BffWebSettings
from sportapp_platform.migrations import upgrade
from sportapp_platform.testing.keys import ed25519_private_key_pem
from sportapp_platform.testing.postgres import temporary_database
from users_management.main import create_app as create_users
from users_management.settings import UsersManagementSettings

USERS_HISTORY = "users_management.infrastructure.persistence_write:migrations"
AFFILIATION_HISTORY = "affiliation_management.infrastructure.persistence:migrations"
POLICIES = "2026-10"
CATALOG = "plans-2026-10-13.v1"
PASSWORD = "contraseña-muy-secreta"
ORIGIN = "http://localhost:4200"
SPORTS_MEDICINE = "services.sports_medicine.access"
SCHEMA = (
    Path(__file__).resolve().parents[4]
    / "contracts"
    / "events"
    / "afiliacion"
    / "suscripcion.cambiada.v1.schema.json"
)


class SubscriptionChangeFlowTest(unittest.TestCase):
    def setUp(self) -> None:
        users_url = self.enterContext(temporary_database("hu016_users"))
        self.affiliation_url = self.enterContext(temporary_database("hu016_affiliation"))
        upgrade(USERS_HISTORY, users_url)
        upgrade(AFFILIATION_HISTORY, self.affiliation_url)
        users = self.enterContext(
            TestClient(
                create_users(
                    UsersManagementSettings(
                        app_env="test",  # type: ignore[arg-type]
                        database_write_url=users_url,  # type: ignore[arg-type]
                        database_read_url=users_url,  # type: ignore[arg-type]
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
                        database_url=self.affiliation_url,  # type: ignore[arg-type]
                        affiliation_expected_catalog_version=CATALOG,
                        users_management_base_url="http://users-management",  # type: ignore[arg-type]
                    ),
                    users_http=users,
                ),
                base_url="http://affiliation-management",
            )
        )
        self.bff = self.enterContext(
            TestClient(
                create_bff(
                    BffWebSettings(
                        app_env="test",  # type: ignore[arg-type]
                        users_management_base_url="http://users-management",  # type: ignore[arg-type]
                        affiliation_management_base_url="http://affiliation-management",  # type: ignore[arg-type]
                        training_management_base_url="http://training.invalid",  # type: ignore[arg-type]
                        complementary_services_base_url="http://services.invalid",  # type: ignore[arg-type]
                        events_management_base_url="http://events.invalid",  # type: ignore[arg-type]
                        cors_allowed_origins=(ORIGIN,),
                    ),
                    users_http=users,
                    affiliation_http=affiliation,
                ),
                base_url="https://bff-web",
                headers={"Origin": ORIGIN},
            )
        )
        engine = create_engine(self.affiliation_url)
        self.addCleanup(engine.dispose)
        self.sessions = sessionmaker(engine)
        self.provision = ProvisionFreeSubscription(
            unit_of_work=lambda: SqlAlchemyProvisioningUnitOfWork(sessionmaker(engine)),
            clock=SystemClock(),
        )

    def register_and_login(self, actor_type: str = "athlete") -> tuple[UUID, dict[str, str]]:
        email = f"{actor_type}-{uuid.uuid4().hex[:8]}@example.com"
        registered = self.bff.post(
            "/v1/users",
            json={
                "full_name": "Ana Pérez",
                "email": email,
                "password": PASSWORD,
                "actor_type": actor_type,
                "accepted_policies_version": POLICIES,
            },
            headers={"Idempotency-Key": str(uuid.uuid4())},
        )
        self.assertEqual(registered.status_code, 201, registered.text)
        session = self.bff.post("/v1/sessions", json={"email": email, "password": PASSWORD})
        self.assertEqual(session.status_code, 200, session.text)
        auth = {"Authorization": f"Bearer {session.json()['access_token']}"}
        return UUID(registered.json()["user_id"]), auth

    def free_athlete(self) -> tuple[UUID, dict[str, str]]:
        user_id, auth = self.register_and_login()
        self.provision(
            RegisteredUser(
                event_id=uuid.uuid4(), user_id=user_id, actor_type="athlete", correlation_id=None
            )
        )
        return user_id, auth

    def change(
        self, auth: dict[str, str], plan: str, periodicity: str, version: int, **headers: str
    ) -> httpx.Response:
        return self.bff.post(
            "/v1/me/subscription/changes",
            json={
                "plan_code": plan,
                "periodicity": periodicity,
                "expected_subscription_version": version,
            },
            headers={**auth, "Idempotency-Key": str(uuid.uuid4()), **headers},
        )

    def capabilities(self, auth: dict[str, str]) -> list[str]:
        response = self.bff.get("/v1/me/capabilities", headers=auth)
        self.assertEqual(response.status_code, 200, response.text)
        return list(response.json()["capabilities"])

    def count(self, model: Any, user_id: UUID) -> int:
        with self.sessions() as session:
            found = session.scalar(
                select(func.count()).select_from(model).where(model.user_id == user_id)
            )
            return found or 0

    def test_catalog_and_provisioning_state_through_the_bff(self) -> None:
        """CA7: el catálogo muestra `FREE` como plan actual con sus siete ofertas; antes del alta
        gratuita, `PROVISIONING` y el cambio → `409 subscription_provisioning` (precondición)."""
        user_id, auth = self.register_and_login()

        provisioning = self.bff.get("/v1/me/subscription", headers=auth)
        rejected = self.change(auth, "PREMIUM", "MONTHLY", 1)
        self.provision(
            RegisteredUser(
                event_id=uuid.uuid4(), user_id=user_id, actor_type="athlete", correlation_id=None
            )
        )
        plans = self.bff.get("/v1/plans", headers=auth)
        current = self.bff.get("/v1/me/subscription", headers=auth)

        self.assertEqual(provisioning.status_code, 200)
        self.assertEqual(provisioning.json()["status"], "PROVISIONING")
        self.assertEqual(
            (rejected.status_code, rejected.json()["code"]), (409, "subscription_provisioning")
        )
        self.assertEqual(plans.status_code, 200, plans.text)
        catalog = plans.json()
        self.assertEqual(
            (catalog["catalog_version"], catalog["current_plan_code"]), (CATALOG, "FREE")
        )
        offers = {
            (plan["plan_code"], offer["periodicity"]): offer["price"]
            for plan in catalog["plans"]
            for offer in plan["offers"]
        }
        self.assertEqual(len(offers), 7)
        self.assertEqual(offers[("PREMIUM", "QUARTERLY")], "14400.00")
        self.assertEqual(plans.headers["Cache-Control"], "no-store")
        self.assertEqual(
            {
                k: current.json()[k]
                for k in ("plan_code", "periodicity", "price", "status", "version")
            },
            {
                "plan_code": "FREE",
                "periodicity": "NONE",
                "price": "0.00",
                "status": "ACTIVE",
                "version": 1,
            },
        )

    def test_change_updates_capabilities_and_returning_to_free_waives_commitments(self) -> None:
        """CA1, CA4, CA6 y CA8: cambio inmediato con compromiso `PENDING_EXTERNAL`, versión
        esperada vencida → `409 version_conflict`, capacidades del plan nuevo por el BFF y regreso a
        `FREE` que condona el compromiso (`WAIVED`)."""
        user_id, auth = self.free_athlete()
        before = self.capabilities(auth)

        premium = self.change(auth, "PREMIUM", "QUARTERLY", 1)
        stale = self.change(auth, "INTERMEDIATE", "MONTHLY", 1)
        after_premium = self.capabilities(auth)
        back = self.change(auth, "FREE", "NONE", 2)
        after_free = self.capabilities(auth)

        self.assertNotIn(SPORTS_MEDICINE, before)
        self.assertEqual(premium.status_code, 200, premium.text)
        self.assertEqual(
            {
                k: premium.json()[k]
                for k in ("plan_code", "periodicity", "price", "origin", "version")
            },
            {
                "plan_code": "PREMIUM",
                "periodicity": "QUARTERLY",
                "price": "14400.00",
                "origin": "USER_CHANGE",
                "version": 2,
            },
        )
        self.assertEqual(premium.headers["Cache-Control"], "no-store")
        self.assertEqual((stale.status_code, stale.json()["code"]), (409, "version_conflict"))
        self.assertIn(SPORTS_MEDICINE, after_premium)
        self.assertEqual((back.status_code, back.json()["plan_code"]), (200, "FREE"))
        self.assertNotIn(SPORTS_MEDICINE, after_free)
        self.assertEqual(sorted(after_free), sorted(before))
        with self.sessions() as session:
            statuses = sorted(
                session.scalars(
                    select(BillingCommitmentRow.status).where(
                        BillingCommitmentRow.user_id == user_id
                    )
                )
            )
        self.assertEqual(statuses, ["WAIVED"])
        self.assertEqual(self.count(SubscriptionRow, user_id), 3)

    def test_retry_with_the_same_key_repeats_the_response_without_a_new_effect(self) -> None:
        """CA3: el reintento devuelve la misma respuesta con `Idempotent-Replayed`, sin otra
        suscripción ni otro evento; la misma clave con otro cuerpo → `idempotency_key_reused`."""
        user_id, auth = self.free_athlete()
        key = str(uuid.uuid4())
        body = {
            "plan_code": "INTERMEDIATE",
            "periodicity": "MONTHLY",
            "expected_subscription_version": 1,
        }

        first = self.bff.post(
            "/v1/me/subscription/changes", json=body, headers={**auth, "Idempotency-Key": key}
        )
        again = self.bff.post(
            "/v1/me/subscription/changes", json=body, headers={**auth, "Idempotency-Key": key}
        )
        reused = self.bff.post(
            "/v1/me/subscription/changes",
            json={**body, "periodicity": "ANNUAL"},
            headers={**auth, "Idempotency-Key": key},
        )

        self.assertEqual((first.status_code, again.status_code), (200, 200))
        self.assertEqual(again.json(), first.json())
        self.assertEqual(again.headers["Idempotent-Replayed"], "true")
        self.assertEqual(
            (reused.status_code, reused.json()["code"]), (409, "idempotency_key_reused")
        )
        self.assertEqual(self.count(SubscriptionRow, user_id), 2)
        with self.sessions() as session:
            changes = session.scalar(
                select(func.count())
                .select_from(OutboxEventRow)
                .where(
                    OutboxEventRow.aggregate_id == user_id,
                    OutboxEventRow.event_type == "suscripcion.cambiada.v1",
                )
            )
        self.assertEqual(changes, 1)

    def test_ineligible_selections_leave_the_subscription_untouched(self) -> None:
        """CA2 y CA3 por el BFF: plan inexistente, `FREE` no seleccionable como pago, periodicidad
        incoherente y el mismo plan vigente se rechazan sin modificar la suscripción."""
        user_id, auth = self.free_athlete()

        missing = self.change(auth, "GOLD", "MONTHLY", 1)
        inconsistent = self.change(auth, "PREMIUM", "NONE", 1)
        unchanged = self.change(auth, "FREE", "NONE", 1)
        current = self.bff.get("/v1/me/subscription", headers=auth).json()

        self.assertEqual((missing.status_code, missing.json()["code"]), (404, "plan_not_found"))
        self.assertEqual(
            (inconsistent.status_code, inconsistent.json()["details"]),
            (422, [{"field": "body.periodicity", "issue": "inconsistent_values"}]),
        )
        self.assertEqual(
            (unchanged.status_code, unchanged.json()["code"]), (409, "subscription_unchanged")
        )
        self.assertEqual((current["plan_code"], current["version"]), ("FREE", 1))
        self.assertEqual(self.count(SubscriptionRow, user_id), 1)

    def test_other_actors_are_rejected_at_the_edge_and_in_the_service(self) -> None:
        """ADR-036 (propiedad): solo el deportista consulta o cambia su plan."""
        _, organizer = self.register_and_login("organizer")
        _, provider = self.register_and_login("service_provider")

        for auth in (organizer, provider):
            for response in (
                self.bff.get("/v1/plans", headers=auth),
                self.bff.get("/v1/me/subscription", headers=auth),
                self.change(auth, "PREMIUM", "MONTHLY", 1),
            ):
                self.assertEqual(
                    (response.status_code, response.json()["code"]), (403, "permission_required")
                )
        self.assertEqual(self.bff.get("/v1/plans").status_code, 401)

    @unittest.skipUnless(os.environ.get("PUBSUB_EMULATOR_HOST"), "requiere el emulador de Pub/Sub")
    def test_change_reaches_the_topic_through_the_outbox(self) -> None:
        """CA5: outbox → `afiliacion-outbox-publisher` → `sportapp.afiliacion.v1`, sobre válido
        contra el esquema, `user_id` como clave de orden y la clave como causa."""
        project_id = f"sportapp-test-{uuid.uuid4().hex[:8]}"
        ensure_resources(project_id)
        subscriber = pubsub_v1.SubscriberClient()
        self.addCleanup(subscriber.close)
        topic = f"projects/{project_id}/topics/{AFFILIATION_TOPIC}"
        listener = subscriber.subscription_path(project_id, f"test-{uuid.uuid4().hex[:6]}")
        subscriber.create_subscription(
            request={"name": listener, "topic": topic, "enable_message_ordering": True}
        )
        user_id, auth = self.free_athlete()
        key, correlation = str(uuid.uuid4()), str(uuid.uuid4())
        changed = self.bff.post(
            "/v1/me/subscription/changes",
            json={
                "plan_code": "PREMIUM",
                "periodicity": "ANNUAL",
                "expected_subscription_version": 1,
            },
            headers={**auth, "Idempotency-Key": key, "X-Correlation-ID": correlation},
        )
        self.assertEqual(changed.status_code, 200, changed.text)

        publisher = PubSubEventPublisher(project_id, AFFILIATION_TOPIC)
        try:
            relay = OutboxRelay(
                SqlAlchemyOutboxStore(self.sessions),
                publisher,
                OutboxPublisherSettings(
                    app_env="test",  # type: ignore[arg-type]
                    database_url=self.affiliation_url,  # type: ignore[arg-type]
                    pubsub_project_id=project_id,
                    worker_id="w-test",
                ),
            )
            published = relay.run_once()
        finally:
            publisher.close()

        received: list[Any] = []
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            # El emulador puede devolver un `pull` vacío aunque el mensaje ya esté publicado.
            received += subscriber.pull(
                request={"subscription": listener, "max_messages": 10}, timeout=10
            ).received_messages
            if any(
                json.loads(m.message.data)["event_type"] == "suscripcion.cambiada.v1"
                for m in received
            ):
                break
        changes = [
            m.message
            for m in received
            if json.loads(m.message.data)["event_type"] == "suscripcion.cambiada.v1"
        ]
        self.assertGreaterEqual(published, 1)
        self.assertEqual(len(changes), 1)
        message = changes[0]
        envelope = json.loads(message.data)
        Draft202012Validator(
            json.loads(SCHEMA.read_text(encoding="utf-8")), format_checker=FormatChecker()
        ).validate(envelope)
        self.assertEqual(message.ordering_key, str(user_id))
        self.assertEqual(
            (envelope["aggregate_id"], envelope["aggregate_version"], envelope["causation_id"]),
            (str(user_id), 2, key),
        )
        self.assertEqual(envelope["correlation_id"], correlation)
        payload = envelope["payload"]
        self.assertEqual(
            (payload["previous_plan_code"], payload["plan_code"], payload["periodicity"]),
            ("FREE", "PREMIUM", "ANNUAL"),
        )
        self.assertNotIn("price", payload)


if __name__ == "__main__":
    unittest.main()
