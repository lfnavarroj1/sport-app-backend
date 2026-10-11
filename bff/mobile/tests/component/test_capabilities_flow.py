"""Componente HU003 CA6-CA10 en móvil: BFF → Usuarios (sesión y JWKS) y BFF → Afiliación.

Servicios reales con PostgreSQL migrado (requiere `TEST_DATABASE_URL`). El alta
gratuita se dispara con el caso de uso de Afiliación sobre el mismo evento que
publicaría Usuarios; el recorrido por Pub/Sub ya lo cubre la prueba de HU001.
"""

import unittest
import uuid
from typing import Any
from uuid import uuid4

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from affiliation_management.application.commands.provision_free_subscription import (
    ProvisionFreeSubscription,
)
from affiliation_management.application.messages import RegisteredUser
from affiliation_management.infrastructure.clock import SystemClock
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
from users_management.main import create_app as create_users
from users_management.settings import UsersManagementSettings

USERS_HISTORY = "users_management.infrastructure.persistence_write:migrations"
AFFILIATION_HISTORY = "affiliation_management.infrastructure.persistence:migrations"
POLICIES = "2026-10"
CATALOG = "plans-2026-10-11.v1"
PASSWORD = "contraseña-muy-secreta"


class CapabilitiesFlowTest(unittest.TestCase):
    def setUp(self) -> None:
        users_url = self.enterContext(temporary_database("hu003_caps_m_users"))
        affiliation_url = self.enterContext(temporary_database("hu003_caps_m_aff"))
        upgrade(USERS_HISTORY, users_url)
        upgrade(AFFILIATION_HISTORY, affiliation_url)
        self.users = users = self.enterContext(
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
        affiliation = self.enterContext(
            TestClient(
                create_affiliation(
                    AffiliationManagementSettings(
                        app_env="test",  # type: ignore[arg-type]
                        database_url=affiliation_url,  # type: ignore[arg-type]
                        affiliation_expected_catalog_version=CATALOG,
                    )
                ),
                base_url="http://affiliation-management",
            )
        )
        self.bff = self.enterContext(
            TestClient(
                create_bff(
                    BffMobileSettings(
                        app_env="test",  # type: ignore[arg-type]
                        users_management_base_url="http://users-management",  # type: ignore[arg-type]
                        affiliation_management_base_url="http://affiliation-management",  # type: ignore[arg-type]
                    ),
                    users_http=users,
                    affiliation_http=affiliation,
                ),
            )
        )
        engine = create_engine(affiliation_url)
        self.addCleanup(engine.dispose)
        self.provision = ProvisionFreeSubscription(
            unit_of_work=lambda: SqlAlchemyProvisioningUnitOfWork(sessionmaker(engine)),
            clock=SystemClock(),
        )

    def register_and_login(self, actor_type: str) -> tuple[str, str]:
        email = f"{actor_type}-{uuid.uuid4().hex[:8]}@example.com"
        registered = self.users.post(
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
        return str(registered.json()["user_id"]), str(session.json()["access_token"])

    def capabilities(self, access_token: str) -> httpx.Response:
        return self.bff.get(
            "/v1/me/capabilities", headers={"Authorization": f"Bearer {access_token}"}
        )

    def test_athlete_goes_from_provisioning_to_free_capabilities(self) -> None:
        """CA7 (y CA8 de HU001): sin suscripción no hay capacidades; con FREE, las once."""
        user_id, access_token = self.register_and_login("athlete")

        before: dict[str, Any] = self.capabilities(access_token).json()
        self.provision(
            RegisteredUser(
                event_id=uuid4(),
                user_id=uuid.UUID(user_id),
                actor_type="athlete",
                correlation_id=None,
            )
        )
        after: dict[str, Any] = self.capabilities(access_token).json()

        self.assertEqual(
            (before["subscription_status"], before["capabilities"]), ("PROVISIONING", [])
        )
        self.assertEqual(after["actor_type"], "athlete")
        self.assertEqual(after["subscription_status"], "ACTIVE")
        self.assertEqual((after["plan_code"], after["catalog_version"]), ("FREE", CATALOG))
        self.assertEqual(len(after["capabilities"]), 11)
        self.assertNotIn("training.realtime.monitor", after["capabilities"])
        self.assertEqual(after["permissions"], [])

    def test_organizer_and_provider_receive_their_permissions(self) -> None:
        """Permisos por actor filtrados por el canal móvil, con identidad emitida por Usuarios."""
        _, organizer = self.register_and_login("organizer")
        _, provider = self.register_and_login("service_provider")

        organizer_view = self.capabilities(organizer).json()
        provider_view = self.capabilities(provider).json()

        self.assertEqual(organizer_view["subscription_status"], "NOT_APPLICABLE")
        # En móvil solo se entregan los permisos de HU aprobadas para móvil.
        self.assertEqual(organizer_view["permissions"], ["training.routes.recommend"])
        self.assertNotIn("events.create", organizer_view["permissions"])
        self.assertIn("events.recommend", provider_view["permissions"])
        self.assertNotIn("services.create", provider_view["permissions"])
        self.assertEqual(provider_view["capabilities"], [])

    def test_without_a_valid_session_nothing_is_disclosed(self) -> None:
        """CA10: sin identidad vigente el backend no entrega menú de negocio."""
        response = self.capabilities("token-inventado")

        self.assertEqual(response.status_code, 401)
        self.assertNotIn("permissions", response.json())


if __name__ == "__main__":
    unittest.main()
