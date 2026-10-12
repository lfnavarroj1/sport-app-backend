"""Componente HU010 en web: BFF → Usuarios → Entrenamientos → outbox → proyección → consulta.

Servicios reales (Usuarios, Afiliación, Entrenamientos y BFF) con PostgreSQL migrado;
requiere `TEST_DATABASE_URL`. Dos piezas se ejecutan con su caso de uso en lugar de
su worker, porque su transporte ya se prueba en la integración de cada servicio:
- el alta `FREE` de Afiliación (HU001) y la publicación/proyección de Entrenamientos
  (Pub/Sub con emulador en `training_management/tests/integration`);
- el directorio de Usuarios se siembra en su base de lectura tal como lo dejará
  `usuarios-read-projector`, que es un refactor pendiente de HU001.
"""

import unittest
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
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
from users_management.infrastructure.persistence_read.models import UserDirectoryRow
from users_management.main import create_app as create_users
from users_management.settings import UsersManagementSettings

USERS_WRITE = "users_management.infrastructure.persistence_write:migrations"
USERS_READ = "users_management.infrastructure.persistence_read:migrations"
AFFILIATION = "affiliation_management.infrastructure.persistence:migrations"
TRAINING_WRITE = "training_management.infrastructure.persistence_write:migrations"
TRAINING_READ = "training_management.infrastructure.persistence_read:migrations"
POLICIES = "2026-10"
CATALOG = "plans-2026-10-11.v1"
PASSWORD = "contraseña-muy-secreta"
ORIGIN = "http://localhost:4200"


def plan_request(athlete_email: str, **overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "athlete_email": athlete_email,
        "title": "Base aeróbica 10K",
        "objectives": ["Correr 10 km continuos"],
        "start_date": "2026-11-02",
        "end_date": "2026-11-29",
        "sessions": [
            {
                "scheduled_date": "2026-11-02",
                "sport": "running",
                "duration_minutes": 40,
                "intensity_rpe": 5,
            },
            {
                "scheduled_date": "2026-11-04",
                "sport": "strength",
                "duration_minutes": 30,
                "intensity_rpe": 6,
            },
        ],
    }
    values.update(overrides)
    return values


class TrainingPlanFlowTest(unittest.TestCase):
    def setUp(self) -> None:
        urls = {
            name: self.enterContext(temporary_database(f"hu010_flow_{name}"))
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
                        database_write_url=urls["training_w"],  # type: ignore[arg-type]
                        database_read_url=urls["training_r"],  # type: ignore[arg-type]
                        users_management_base_url="http://users-management",  # type: ignore[arg-type]
                        affiliation_management_base_url="http://affiliation-management",  # type: ignore[arg-type]
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
        # Lo que dejaría `usuarios-read-projector` al proyectar `usuario.registrado.v1`.
        with sessionmaker(self.engines["users_r"])() as session, session.begin():
            session.add(
                UserDirectoryRow(
                    user_id=uuid.UUID(user_id),
                    email=email,
                    actor_type=actor_type,
                    status="active",
                    aggregate_version=1,
                    projected_at=datetime.now(UTC),
                )
            )
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
        session_response = self.bff.post(
            "/v1/sessions", json={"email": email, "password": PASSWORD}
        )
        self.assertEqual(session_response.status_code, 200, session_response.text)
        return {
            "user_id": user_id,
            "email": email,
            "token": session_response.json()["access_token"],
        }

    def auth(self, user: dict[str, str], **extra: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {user['token']}", **extra}

    def create_plan(self, user: dict[str, str], body: dict[str, Any], key: str) -> httpx.Response:
        return self.bff.post(
            "/v1/training-plans", json=body, headers=self.auth(user, **{"Idempotency-Key": key})
        )

    def project_outbox(self) -> int:
        """`entrenamientos-outbox-publisher` + `entrenamientos-read-projector` sin Pub/Sub;
        devuelve cuántos eventos de plan se publicaron."""
        envelopes: list[dict[str, Any]] = []

        class Capture:
            def publish(self, envelope: dict[str, Any], ordering_key: str) -> None:
                envelopes.append(envelope)

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
        # Solo cuentan los eventos de plan; las autorizaciones también se publican (HU011).
        return sum(e["event_type"] == "plan.entrenamiento_creado.v1" for e in envelopes)

    def test_hu010_vertical_backend_flow(self) -> None:
        """CA1-CA5 por el canal web de punta a punta en el backend."""
        athlete, professional = self.athlete, self.professional
        body = plan_request(athlete["email"])

        # CA2: sin autorización previa del deportista no hay asignación.
        before = self.create_plan(professional, body, str(uuid.uuid4()))
        self.assertEqual(
            (before.status_code, before.json()["code"]), (403, "athlete_authorization_required")
        )

        granted = self.bff.post(
            "/v1/me/training-authorizations",
            json={"professional_email": professional["email"]},
            headers=self.auth(athlete),
        )
        self.assertEqual(granted.status_code, 201, granted.text)
        self.assertEqual(granted.json()["professional_id"], professional["user_id"])

        # CA1 y CA5: crear y reintentar con la misma clave.
        key = str(uuid.uuid4())
        created = self.create_plan(professional, body, key)
        retried = self.create_plan(professional, body, key)
        self.assertEqual(created.status_code, 201, created.text)
        plan = created.json()
        self.assertEqual(plan["athlete_id"], athlete["user_id"])
        self.assertEqual(plan["professional_id"], professional["user_id"])
        self.assertEqual((plan["start_date"], plan["end_date"]), ("2026-11-02", "2026-11-29"))
        self.assertEqual(retried.json(), plan)
        self.assertEqual(retried.headers["Idempotent-Replayed"], "true")

        # CA3: reglas incumplidas sin persistir.
        invalid = self.create_plan(
            professional,
            plan_request(athlete["email"], start_date="2026-11-29", end_date="2026-11-02"),
            str(uuid.uuid4()),
        )
        self.assertEqual(
            (invalid.status_code, invalid.json()["code"]), (422, "training_plan_invalid")
        )

        # CA4: consultable tras la proyección, sin datos de perfil; evento publicado.
        plan_url = f"/v1/training-plans/{plan['plan_id']}"
        not_yet = self.bff.get(plan_url, headers=self.auth(professional))
        self.assertEqual(not_yet.status_code, 404, "la consulta no espera la proyección")
        self.assertEqual(self.project_outbox(), 1, "un único evento: el reintento no duplicó")
        by_professional = self.bff.get(plan_url, headers=self.auth(professional))
        by_athlete = self.bff.get(plan_url, headers=self.auth(athlete))
        self.assertEqual(by_professional.status_code, 200, by_professional.text)
        self.assertEqual(by_professional.json(), plan)
        self.assertEqual(by_athlete.json(), plan)
        self.assertNotIn(athlete["email"], by_athlete.text)
        self.assertNotIn("Persona Sintética", by_athlete.text)

        # CA2: otro profesional no ve el plan; tras revocar, no se asignan más planes.
        stranger = self.register("service_provider")
        self.assertEqual(self.bff.get(plan_url, headers=self.auth(stranger)).status_code, 404)
        revoked = self.bff.delete(
            f"/v1/me/training-authorizations/{professional['user_id']}", headers=self.auth(athlete)
        )
        after = self.create_plan(professional, body, str(uuid.uuid4()))
        self.assertEqual(revoked.status_code, 204)
        self.assertEqual(after.json()["code"], "athlete_authorization_required")

    def project_finished_sessions(self, athlete_id: str, *specs: tuple[str, datetime]) -> None:
        """Publica en la lectura `entrenamiento.finalizado.v1` sintéticos, como hará HU032."""
        project = TrainingProjector(
            unit_of_work=lambda: SqlAlchemyProjectionUnitOfWork(
                sessionmaker(self.engines["training_r"])
            ),
            clock=SystemClock(),
        )
        for sport, ended_at in specs:
            session_id = str(uuid.uuid4())
            project(
                parse_training_event(
                    {
                        "event_id": str(uuid.uuid4()),
                        "event_type": "entrenamiento.finalizado.v1",
                        "occurred_at": ended_at.isoformat(),
                        "producer": "training-management",
                        "aggregate_type": "training_session",
                        "aggregate_id": session_id,
                        "aggregate_version": 2,
                        "correlation_id": None,
                        "causation_id": None,
                        "schema_version": 1,
                        "payload": {
                            "session_id": session_id,
                            "athlete_id": athlete_id,
                            "plan_id": None,
                            "sport": sport,
                            "started_at": (ended_at - timedelta(minutes=40)).isoformat(),
                            "ended_at": ended_at.isoformat(),
                            "duration_seconds": 2400,
                            "status": "FINISHED",
                        },
                    }
                )
            )

    def test_hu011_history_backend_flow(self) -> None:
        """HU011 CA1-CA4 por el canal web: autorización proyectada, filtros, cursor y revocación."""
        athlete, professional = self.athlete, self.professional
        history_url = f"/v1/athletes/{athlete['user_id']}/training-history"

        before = self.bff.get(history_url, headers=self.auth(professional))
        self.assertEqual(
            (before.status_code, before.json()["code"]), (403, "athlete_authorization_required")
        )

        granted = self.bff.post(
            "/v1/me/training-authorizations",
            json={"professional_email": professional["email"]},
            headers=self.auth(athlete),
        )
        self.assertEqual(granted.status_code, 201, granted.text)
        self.project_outbox()  # la autorización llega a la lectura por la outbox y el proyector

        empty = self.bff.get(history_url, headers=self.auth(professional))
        self.assertEqual(empty.status_code, 200, empty.text)
        self.assertEqual((empty.json()["items"], empty.json()["next_cursor"]), ([], None))
        self.assertIn("running", empty.json()["filters"]["sports"])  # CA2

        day = datetime(2026, 10, 5, 7, 0, tzinfo=UTC)
        self.project_finished_sessions(
            athlete["user_id"],
            ("running", day),
            ("cycling", day + timedelta(days=1)),
            ("running", day + timedelta(days=2)),
        )
        self.project_finished_sessions(str(uuid.uuid4()), ("running", day))  # otro deportista

        first = self.bff.get(
            "/v1/training-history", params={"limit": 2}, headers=self.auth(athlete)
        )
        second = self.bff.get(
            "/v1/training-history",
            params={"limit": 2, "cursor": first.json()["next_cursor"]},
            headers=self.auth(athlete),
        )
        cycling = self.bff.get(
            history_url, params={"sport": "cycling"}, headers=self.auth(professional)
        )
        self.assertEqual(first.status_code, 200, first.text)
        ids = [i["session_id"] for i in first.json()["items"] + second.json()["items"]]
        self.assertEqual(len(ids), 3)  # CA4: sin pérdidas ni repeticiones
        self.assertEqual(len(set(ids)), 3)
        self.assertIsNone(second.json()["next_cursor"])
        ended = [i["ended_at"] for i in first.json()["items"] + second.json()["items"]]
        self.assertEqual(ended, sorted(ended, reverse=True))  # CA1: por fecha
        self.assertEqual([i["sport"] for i in cycling.json()["items"]], ["cycling"])

        stranger = self.register("service_provider")
        self.assertEqual(
            self.bff.get(history_url, headers=self.auth(stranger)).json()["code"],
            "athlete_authorization_required",
        )  # CA3
        self.bff.delete(
            f"/v1/me/training-authorizations/{professional['user_id']}", headers=self.auth(athlete)
        )
        self.project_outbox()
        after = self.bff.get(history_url, headers=self.auth(professional))
        self.assertEqual(after.json()["code"], "athlete_authorization_required")

    def test_roles_without_permission_are_rejected(self) -> None:
        """CA2: un deportista u organizador no puede crear ni asignar planes."""
        organizer = self.register("organizer")
        for user in (self.athlete, organizer):
            with self.subTest(user=user["email"].split("-")[0]):
                response = self.create_plan(
                    user, plan_request(self.athlete["email"]), str(uuid.uuid4())
                )
                self.assertEqual(
                    (response.status_code, response.json()["code"]), (403, "permission_required")
                )


if __name__ == "__main__":
    unittest.main()
