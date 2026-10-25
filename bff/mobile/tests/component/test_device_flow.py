"""Componente HU037 en móvil: BFF → Usuarios (sesión y JWKS) → Afiliación (capacidades
`devices.measurement.connect` y `training.realtime.monitor` del plan `INTERMEDIATE`) →
Entrenamientos → `athlete_device` y `training_measurement`.

Servicios reales con PostgreSQL migrado (requiere `TEST_DATABASE_URL`). El plan `FREE` se activa
con el caso de uso real del alta gratuita (ADR-024) y el cambio a `INTERMEDIATE` con la operación
real de HU016. La vinculación y la ingestión no emiten eventos propios, así que no interviene
Pub/Sub. Recorrido de ADR-044: vincular → iniciar sesión (HU032) → lotes aceptados con
`duplicates` en el reenvío → revocar → `409 device_revoked`.
"""

import hashlib
import unittest
import uuid
from datetime import UTC, datetime, timedelta
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
from bff_mobile.main import create_app as create_bff
from bff_mobile.settings import BffMobileSettings
from sportapp_platform.migrations import upgrade
from sportapp_platform.testing.keys import ed25519_private_key_pem
from sportapp_platform.testing.postgres import temporary_database
from training_management.infrastructure.persistence_write.models import (
    AthleteDeviceRow,
    TrainingMeasurementRow,
)
from training_management.main import create_app as create_training
from training_management.settings import TrainingManagementSettings
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
DEVICES = "/v1/me/devices"


def utc_seconds(delta: timedelta) -> str:
    instant = datetime.now(UTC).replace(microsecond=0) + delta
    return instant.isoformat().replace("+00:00", "Z")


def heart_rate(*seconds_ago: int, value: int = 132) -> list[dict[str, Any]]:
    return [
        {
            "measured_at": utc_seconds(-timedelta(seconds=ago)),
            "type": "HEART_RATE",
            "value": value,
            "unit": "bpm",
        }
        for ago in seconds_ago
    ]


class DeviceFlowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.urls = {
            name: self.enterContext(temporary_database(f"hu037_{name}")) for name in HISTORIES
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
        self.affiliation = self.enterContext(
            TestClient(
                create_affiliation(
                    AffiliationManagementSettings(
                        app_env="test",  # type: ignore[arg-type]
                        database_url=self.urls["affiliation"],  # type: ignore[arg-type]
                        affiliation_expected_catalog_version=CATALOG,
                        users_management_base_url="http://users-management",  # type: ignore[arg-type]
                    ),
                    users_http=self.users,
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
                    affiliation_http=self.affiliation,
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
                    affiliation_http=self.affiliation,
                    training_http=training,
                ),
                base_url="https://bff-mobile",
            )
        )
        self.engine = create_engine(self.urls["training_w"])
        self.addCleanup(self.engine.dispose)
        affiliation_engine = create_engine(self.urls["affiliation"])
        self.addCleanup(affiliation_engine.dispose)
        self.provision = ProvisionFreeSubscription(
            unit_of_work=lambda: SqlAlchemyProvisioningUnitOfWork(sessionmaker(affiliation_engine)),
            clock=AffiliationClock(),
        )

    def athlete(self) -> dict[str, str]:
        email = f"athlete-{uuid.uuid4().hex[:8]}@example.com"
        registered = self.users.post(
            "/v1/users",
            json={
                "full_name": "Persona Sintética",
                "email": email,
                "password": PASSWORD,
                "actor_type": "athlete",
                "accepted_policies_version": POLICIES,
            },
            headers={"Idempotency-Key": str(uuid.uuid4())},
        )
        self.assertEqual(registered.status_code, 201, registered.text)
        user_id = registered.json()["user_id"]
        self.provision(RegisteredUser(uuid.uuid4(), uuid.UUID(user_id), "athlete", None))
        login = self.bff.post("/v1/sessions", json={"email": email, "password": PASSWORD})
        self.assertEqual(login.status_code, 200, login.text)
        return {"user_id": user_id, "Authorization": f"Bearer {login.json()['access_token']}"}

    def upgrade_to_intermediate(self, athlete: dict[str, str]) -> None:
        changed = self.affiliation.post(
            "/v1/me/subscription/changes",
            json={
                "plan_code": "INTERMEDIATE",
                "periodicity": "MONTHLY",
                "expected_subscription_version": 1,
            },
            headers={
                "Authorization": athlete["Authorization"],
                "Idempotency-Key": str(uuid.uuid4()),
            },
        )
        self.assertEqual(changed.status_code, 200, changed.text)

    def link(self, athlete: dict[str, str], device_id: str, key: str) -> httpx.Response:
        return self.bff.post(
            DEVICES,
            json={
                "device_id": device_id,
                "display_name": "Banda pecho",
                "device_type": "HEART_RATE_MONITOR",
                "protocol": "BLE_HRS",
            },
            headers={"Authorization": athlete["Authorization"], "Idempotency-Key": key},
        )

    def send(self, athlete: dict[str, str], session_id: str, body: Any) -> httpx.Response:
        return self.bff.post(
            f"/v1/training-sessions/{session_id}/measurements",
            json=body,
            headers={"Authorization": athlete["Authorization"]},
        )

    def count(self, model: Any) -> int:
        with sessionmaker(self.engine)() as session:
            return session.scalar(select(func.count()).select_from(model)) or 0

    def test_link_monitor_send_batches_and_revoke_through_the_mobile_channel(self) -> None:
        """CA1, CA3-CA6 por el canal móvil, con identidad, plan y persistencia reales."""
        athlete = self.athlete()
        self.upgrade_to_intermediate(athlete)
        device_id = hashlib.sha256(f"C0:FF:EE:00:11:22{athlete['user_id']}".encode()).hexdigest()

        key = str(uuid.uuid4())
        linked = self.link(athlete, device_id, key)
        self.assertEqual(linked.status_code, 201, linked.text)
        self.assertEqual(linked.headers["Location"], f"{DEVICES}/{device_id}")
        self.assertEqual(
            {
                name: linked.json()[name]
                for name in ("status", "version", "device_type", "protocol")
            },
            {
                "status": "ACTIVE",
                "version": 1,
                "device_type": "HEART_RATE_MONITOR",
                "protocol": "BLE_HRS",
            },
        )
        replay = self.link(athlete, device_id, key)
        self.assertEqual((replay.status_code, replay.headers["Idempotent-Replayed"]), (201, "true"))

        session_id = str(uuid.uuid4())
        started = self.bff.post(
            "/v1/training-sessions",
            json={
                "session_id": session_id,
                "sport": "cycling",
                "started_at": utc_seconds(-timedelta(minutes=5)),
            },
            headers={
                "Authorization": athlete["Authorization"],
                "Idempotency-Key": str(uuid.uuid4()),
            },
        )
        self.assertEqual(started.status_code, 201, started.text)

        batch = {
            "device_id": device_id,
            "samples": [*heart_rate(5, 4, 3, 2, 1), *heart_rate(0, value=400)],
        }
        first = self.send(athlete, session_id, batch)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.headers["Cache-Control"], "no-store")
        self.assertEqual(
            {
                name: first.json()[name]
                for name in ("received", "accepted", "duplicates", "rejected")
            },
            {
                "received": 6,
                "accepted": 5,
                "duplicates": 0,
                "rejected": [{"index": 5, "issue": "out_of_range"}],
            },
        )
        resent = self.send(athlete, session_id, batch).json()
        self.assertEqual((resent["accepted"], resent["duplicates"]), (0, 5))
        self.assertEqual(self.count(TrainingMeasurementRow), 5)

        listed = self.bff.get(DEVICES, headers={"Authorization": athlete["Authorization"]})
        self.assertEqual([item["device_id"] for item in listed.json()["items"]], [device_id])

        revoked = self.bff.delete(
            f"{DEVICES}/{device_id}", headers={"Authorization": athlete["Authorization"]}
        )
        self.assertEqual((revoked.status_code, revoked.json()["status"]), (200, "REVOKED"))
        rejected = self.send(
            athlete, session_id, {"device_id": device_id, "samples": heart_rate(0)}
        )
        self.assertEqual((rejected.status_code, rejected.json()["code"]), (409, "device_revoked"))
        self.assertEqual(self.count(TrainingMeasurementRow), 5)
        self.assertEqual(self.count(AthleteDeviceRow), 1)

    def test_free_athlete_cannot_link_devices_nor_send_measurements(self) -> None:
        """Elegibilidad: el plan `FREE` no incluye las capacidades de ADR-044."""
        athlete = self.athlete()
        device_id = hashlib.sha256(f"C0:FF:EE:00:11:22{athlete['user_id']}".encode()).hexdigest()
        responses = [
            self.link(athlete, device_id, str(uuid.uuid4())),
            self.bff.get(DEVICES, headers={"Authorization": athlete["Authorization"]}),
            self.send(
                athlete, str(uuid.uuid4()), {"device_id": device_id, "samples": heart_rate(0)}
            ),
        ]
        for response in responses:
            self.assertEqual(
                (response.status_code, response.json()["code"]), (403, "capability_required")
            )
        self.assertEqual(self.count(AthleteDeviceRow), 0)


if __name__ == "__main__":
    unittest.main()
