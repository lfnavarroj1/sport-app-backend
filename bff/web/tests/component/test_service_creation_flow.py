"""Componente HU015 en web: BFF → Usuarios (sesión y JWKS) → Servicios → PostgreSQL.

Servicios reales con PostgreSQL migrado (requiere `TEST_DATABASE_URL`). HU015 no
publica eventos, así que el recorrido termina en la base del dominio.
"""

import unittest
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from bff_web.main import create_app as create_bff
from bff_web.settings import BffWebSettings
from complementary_services.infrastructure.persistence.models import ServiceRow
from complementary_services.main import create_app as create_services
from complementary_services.settings import ComplementaryServicesSettings
from sportapp_platform.migrations import upgrade
from sportapp_platform.testing.keys import ed25519_private_key_pem
from sportapp_platform.testing.postgres import temporary_database
from users_management.main import create_app as create_users
from users_management.settings import UsersManagementSettings

USERS = "users_management.infrastructure.persistence_write:migrations"
SERVICES = "complementary_services.infrastructure.persistence:migrations"
POLICIES = "2026-10"
PASSWORD = "contraseña-muy-secreta"
ORIGIN = "http://localhost:4200"


def offer(**overrides: Any) -> dict[str, Any]:
    day = (datetime.now(UTC) + timedelta(days=10)).date().isoformat()
    values: dict[str, Any] = {
        "name": "  Valoración deportiva inicial  ",
        "description": "Valoración presencial para orientar el plan deportivo.",
        "category": "SPORTS_MEDICINE",
        "modality": "IN_PERSON",
        "price": {"amount": "120000.00", "currency": "COP", "billing_unit": "SESSION"},
        "availability": [
            {
                "starts_at": f"{day}T09:00:00-05:00",
                "ends_at": f"{day}T10:00:00-05:00",
                "time_zone": "America/Bogota",
                "capacity": 1,
            }
        ],
    }
    values.update(overrides)
    return values


class ServiceCreationFlowTest(unittest.TestCase):
    def setUp(self) -> None:
        users_url = self.enterContext(temporary_database("hu015_flow_users"))
        services_url = self.enterContext(temporary_database("hu015_flow_services"))
        upgrade(USERS, users_url)
        upgrade(SERVICES, services_url)
        users = self.enterContext(
            TestClient(
                create_users(
                    UsersManagementSettings(
                        app_env="test",  # type: ignore[arg-type]
                        database_write_url=users_url,  # type: ignore[arg-type]
                        database_read_url=users_url,  # type: ignore[arg-type]
                        registration_policies_version=POLICIES,
                        jwt_private_key=ed25519_private_key_pem(),  # type: ignore[arg-type]
                    )
                ),
                base_url="http://users-management",
            )
        )
        services = self.enterContext(
            TestClient(
                create_services(
                    ComplementaryServicesSettings(
                        app_env="test",  # type: ignore[arg-type]
                        database_url=services_url,  # type: ignore[arg-type]
                        users_management_base_url="http://users-management",  # type: ignore[arg-type]
                    ),
                    users_http=users,
                ),
                base_url="http://complementary-services",
            )
        )
        self.bff = self.enterContext(
            TestClient(
                create_bff(
                    BffWebSettings(
                        app_env="test",  # type: ignore[arg-type]
                        users_management_base_url="http://users-management",  # type: ignore[arg-type]
                        affiliation_management_base_url="http://affiliation.invalid",  # type: ignore[arg-type]
                        training_management_base_url="http://training.invalid",  # type: ignore[arg-type]
                        complementary_services_base_url="http://complementary-services",  # type: ignore[arg-type]
                        events_management_base_url="http://events.invalid",  # type: ignore[arg-type]
                        cors_allowed_origins=(ORIGIN,),
                    ),
                    users_http=users,
                    services_http=services,
                ),
                base_url="https://bff-web",
                headers={"Origin": ORIGIN},
            )
        )
        engine = create_engine(services_url)
        self.addCleanup(engine.dispose)
        self.sessions = sessionmaker(engine)

    def session_for(self, actor_type: str) -> dict[str, str]:
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
        login = self.bff.post("/v1/sessions", json={"email": email, "password": PASSWORD})
        return {
            "user_id": registered.json()["user_id"],
            "Authorization": f"Bearer {login.json()['access_token']}",
        }

    def create(self, user: dict[str, str], body: Any, key: str) -> httpx.Response:
        return self.bff.post(
            "/v1/services",
            json=body,
            headers={"Authorization": user["Authorization"], "Idempotency-Key": key},
        )

    def test_hu015_vertical_backend_flow(self) -> None:
        """CA1-CA7 por el canal web de punta a punta en el backend."""
        provider = self.session_for("service_provider")
        key = str(uuid.uuid4())

        created = self.create(provider, offer(), key)
        retried = self.create(provider, offer(), key)
        reused = self.create(provider, offer(name="Otro servicio distinto"), key)

        self.assertEqual(created.status_code, 201, created.text)
        body = created.json()
        self.assertEqual(body["provider_id"], provider["user_id"])  # CA1: del token
        self.assertEqual((body["status"], body["version"]), ("DRAFT", 1))
        self.assertEqual(body["name"], "Valoración deportiva inicial")  # recortado
        self.assertTrue(body["availability"][0]["starts_at"].endswith("Z"))  # UTC
        self.assertEqual(body["availability"][0]["time_zone"], "America/Bogota")
        self.assertEqual(retried.json(), body)  # CA4
        self.assertEqual(retried.headers["Idempotent-Replayed"], "true")
        self.assertEqual(
            (reused.status_code, reused.json()["code"]), (409, "idempotency_key_reused")
        )

        invalid = self.create(
            provider,
            offer(
                provider_id=str(uuid.uuid4()),
                price={"amount": "1.234", "currency": "USD", "billing_unit": "SESSION"},
            ),
            str(uuid.uuid4()),
        )
        self.assertEqual(invalid.status_code, 422)  # CA3
        self.assertCountEqual(
            invalid.json()["details"],
            [
                {"field": "provider_id", "issue": "field_not_allowed"},
                {"field": "price.amount", "issue": "too_many_decimals"},
                {"field": "price.currency", "issue": "unsupported_currency"},
            ],
        )

        with self.sessions() as session:
            rows = list(session.scalars(select(ServiceRow)))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].status, "DRAFT")  # CA5: fuera del catálogo público

    def test_other_actors_cannot_create(self) -> None:
        """CA2: el filtro de borde y el servicio rechazan a deportistas y organizadores."""
        for actor in ("athlete", "organizer"):
            with self.subTest(actor=actor):
                user = self.session_for(actor)
                response = self.create(user, offer(), str(uuid.uuid4()))
                self.assertEqual(
                    (response.status_code, response.json()["code"]), (403, "permission_required")
                )


if __name__ == "__main__":
    unittest.main()
