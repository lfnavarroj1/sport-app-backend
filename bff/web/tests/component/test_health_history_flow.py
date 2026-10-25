"""Componente HU012 en web: reporte de HU042 → outbox → proyección → BFF web → Entrenamientos
→ consulta del deportista y del profesional autorizado (ADR-037).

Servicios reales (Usuarios, Afiliación, Entrenamientos y BFF) con PostgreSQL migrado;
requiere `TEST_DATABASE_URL`. Como en `test_training_plan_flow.py`, el alta `FREE`, el
directorio de Usuarios y la publicación/proyección de Entrenamientos se ejecutan con sus casos
de uso (su transporte por Pub/Sub se prueba en la integración de cada servicio). Los reportes
nacen con la operación real de HU042 de Entrenamientos (el canal móvil la expone en
`bff-mobile`); la autorización, con la operación web de HU010.
"""

import json
import logging
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
CONSENT = "local-dev"
ITEM_FIELDS = {
    "report_id",
    "type",
    "body_area",
    "severity",
    "occurred_at",
    "expected_recovery_days",
    "status",
    "session_id",
}
# Valores sintéticos distinguibles para comprobar que no aparecen en logs (CA5); sin cifras,
# que podrían coincidir con `duration_ms` o un UUID.
SENSITIVE = ("INCAPACITY", "ANKLE_FOOT")


def utc_seconds(delta: timedelta) -> str:
    return (datetime.now(UTC) + delta).strftime("%Y-%m-%dT%H:%M:%SZ")


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


class HealthHistoryFlowTest(unittest.TestCase):
    def setUp(self) -> None:
        urls = {
            name: self.enterContext(temporary_database(f"hu012_flow_{name}"))
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
                        health_data_consent_version=CONSENT,
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

    def project_outbox(self) -> None:
        """`entrenamientos-outbox-publisher` + `entrenamientos-read-projector` sin Pub/Sub."""
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
            project(parse_training_event(json.loads(json.dumps(envelope))))

    def auth(self, user: dict[str, str]) -> dict[str, str]:
        return {"Authorization": f"Bearer {user['token']}"}

    def report(self, athlete: dict[str, str], hours_ago: int, **overrides: Any) -> str:
        """HU042: el reporte nace con la operación real de Entrenamientos."""
        body = {
            "report_id": str(uuid.uuid4()),
            "type": "DISCOMFORT",
            "body_area": "KNEE",
            "severity": "LOW",
            "occurred_at": utc_seconds(-timedelta(hours=hours_ago)),
            "consent": {"version": CONSENT},
            **overrides,
        }
        created = self.training.post(
            "/v1/me/health-reports",
            json=body,
            headers={**self.auth(athlete), "Idempotency-Key": str(uuid.uuid4())},
        )
        self.assertEqual(created.status_code, 201, created.text)
        return str(body["report_id"])

    def own(self, **params: Any) -> httpx.Response:
        return self.bff.get("/v1/me/health-reports", params=params, headers=self.auth(self.athlete))

    def test_athlete_history_backend_flow(self) -> None:
        """CA1, CA4 y CA6 por el canal web: vacío explícito, filtros, orden y cursor."""
        empty = self.own()
        self.assertEqual(empty.status_code, 200, empty.text)
        self.assertEqual(empty.json(), {"items": [], "next_cursor": None})  # CA4

        oldest = self.report(self.athlete, 50)
        injury = self.report(self.athlete, 30, type="INJURY", severity="HIGH")
        newest = self.report(self.athlete, 2, expected_recovery_days=4)
        other = self.register("athlete")
        self.report(other, 1)
        self.project_outbox()

        first = self.own(limit=2)
        second = self.own(limit=2, cursor=first.json()["next_cursor"])
        injuries = self.own(type="INJURY")
        high = self.own(severity="HIGH")
        today = datetime.now(UTC).date()
        period = self.own(**{"from": today.isoformat(), "to": today.isoformat()})
        bad_period = self.own(**{"from": "2026-10-09", "to": "2026-10-08"})
        bad_cursor = self.own(cursor="basura")

        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.headers["Cache-Control"], "no-store")
        items = first.json()["items"] + second.json()["items"]
        self.assertEqual([i["report_id"] for i in items], [newest, injury, oldest])  # CA1
        self.assertIsNone(second.json()["next_cursor"])  # CA6
        self.assertEqual(set(items[0]), ITEM_FIELDS)
        self.assertEqual(
            (items[0]["status"], items[0]["expected_recovery_days"], items[0]["session_id"]),
            ("OPEN", 4, None),
        )
        self.assertTrue(items[0]["occurred_at"].endswith("Z"))
        self.assertEqual([i["report_id"] for i in injuries.json()["items"]], [injury])
        self.assertEqual([i["report_id"] for i in high.json()["items"]], [injury])
        self.assertIn(newest, [i["report_id"] for i in period.json()["items"]])
        self.assertNotIn(oldest, [i["report_id"] for i in period.json()["items"]])
        for response, field in ((bad_period, "query.to"), (bad_cursor, "query.cursor")):
            self.assertEqual(response.status_code, 422)
            self.assertEqual(response.json()["code"], "validation_error")
            self.assertEqual(response.json()["details"][0]["field"], field)

    def test_professional_history_backend_flow(self) -> None:
        """CA2, CA3 y CA5: autorización proyectada, 403 sin enumeración, auditoría sin datos."""
        athlete, professional = self.athlete, self.professional
        url = f"/v1/athletes/{athlete['user_id']}/health-reports"
        reported = self.report(
            athlete, 3, type="INCAPACITY", body_area="ANKLE_FOOT", expected_recovery_days=187
        )
        self.project_outbox()
        capture = _Capture()
        root = logging.getLogger()
        previous = root.level
        root.addHandler(capture)
        root.setLevel(logging.DEBUG)
        self.addCleanup(root.setLevel, previous)
        self.addCleanup(root.removeHandler, capture)

        before = self.bff.get(url, headers=self.auth(professional))
        unknown = self.bff.get(
            f"/v1/athletes/{uuid.uuid4()}/health-reports", headers=self.auth(professional)
        )
        granted = self.bff.post(
            "/v1/me/training-authorizations",
            json={"professional_email": professional["email"]},
            headers=self.auth(athlete),
        )
        self.assertEqual(granted.status_code, 201, granted.text)
        self.project_outbox()  # la autorización llega a la lectura por la outbox y el proyector
        allowed = self.bff.get(url, headers=self.auth(professional))
        stranger = self.register("service_provider")
        foreign = self.bff.get(url, headers=self.auth(stranger))
        organizer = self.register("organizer")
        by_organizer = self.bff.get(url, headers=self.auth(organizer))
        self.bff.delete(
            f"/v1/me/training-authorizations/{professional['user_id']}", headers=self.auth(athlete)
        )
        self.project_outbox()
        revoked = self.bff.get(url, headers=self.auth(professional))

        for response in (before, unknown, foreign, revoked):
            self.assertEqual(
                (response.status_code, response.json()["code"]),
                (403, "athlete_authorization_required"),
            )
        self.assertEqual(before.json()["message"], unknown.json()["message"])
        self.assertEqual(
            (by_organizer.status_code, by_organizer.json()["code"]), (403, "permission_required")
        )
        self.assertEqual(allowed.status_code, 200, allowed.text)
        [item] = allowed.json()["items"]
        self.assertEqual(set(item), ITEM_FIELDS)
        self.assertEqual(
            (item["report_id"], item["type"], item["body_area"], item["expected_recovery_days"]),
            (reported, "INCAPACITY", "ANKLE_FOOT", 187),
        )
        audits = [
            record.fields  # type: ignore[attr-defined]
            for record in capture.records
            if record.getMessage() == "audit.health_history_access"
        ]
        self.assertEqual(
            [a["result"] for a in audits],
            [
                "rejected:athlete_authorization_required",
                "rejected:athlete_authorization_required",
                "granted",
                "rejected:athlete_authorization_required",
                "rejected:athlete_authorization_required",
            ],
        )  # el organizador se rechaza en el borde del BFF y no llega al servicio
        for record in capture.records:
            text = record.getMessage() + json.dumps(getattr(record, "fields", {}), default=str)
            for value in SENSITIVE:
                self.assertNotIn(value, text, record.getMessage())  # CA5


if __name__ == "__main__":
    unittest.main()
