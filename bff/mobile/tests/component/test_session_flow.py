"""Componente HU003 CA6 (parte backend): BFF móvil → users_management → PostgreSQL.

El registro móvil no está aprobado: la cuenta se crea por el servicio, como lo
haría el canal web. El inicio de sesión y la renovación pasan por `bff_mobile`
con el mismo contrato funcional que el canal web.
"""

import unittest
import uuid

from fastapi.testclient import TestClient

from bff_mobile.main import create_app as create_bff
from bff_mobile.settings import BffMobileSettings
from sportapp_platform.migrations import upgrade
from sportapp_platform.testing.keys import ed25519_private_key_pem
from sportapp_platform.testing.postgres import temporary_database
from sportapp_platform.tokens import AccessTokenVerifier
from users_management.main import create_app as create_users
from users_management.settings import UsersManagementSettings

WRITE_HISTORY = "users_management.infrastructure.persistence_write:migrations"
POLICIES = "2026-10"
EMAIL = "ana.movil@example.com"
PASSWORD = "contraseña-muy-secreta"


class MobileSessionFlowTest(unittest.TestCase):
    def setUp(self) -> None:
        url = self.enterContext(temporary_database("bff_mobile_hu003"))
        upgrade(WRITE_HISTORY, url)
        self.users = self.enterContext(
            TestClient(
                create_users(
                    UsersManagementSettings(
                        app_env="test",  # type: ignore[arg-type]
                        database_write_url=url,  # type: ignore[arg-type]
                        database_read_url=url,  # type: ignore[arg-type]
                        registration_policies_version=POLICIES,
                        jwt_private_key=ed25519_private_key_pem(),  # type: ignore[arg-type]
                    )
                ),
                base_url="http://users-management",
            )
        )
        settings = BffMobileSettings(
            app_env="test",  # type: ignore[arg-type]
            users_management_base_url="http://users-management",  # type: ignore[arg-type]
        )
        self.bff = self.enterContext(TestClient(create_bff(settings, users_http=self.users)))
        registered = self.users.post(
            "/v1/users",
            json={
                "full_name": "Ana Pérez",
                "email": EMAIL,
                "password": PASSWORD,
                "actor_type": "athlete",
                "accepted_policies_version": POLICIES,
            },
            headers={"Idempotency-Key": str(uuid.uuid4())},
        )
        self.assertEqual(registered.status_code, 201, registered.text)
        self.user_id = registered.json()["user_id"]

    def test_mobile_login_and_refresh_through_bff_mobile(self) -> None:
        response = self.bff.post("/v1/sessions", json={"email": EMAIL, "password": PASSWORD})

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        session = response.json()
        jwks = self.users.get("/.well-known/jwks.json").json()
        verifier = AccessTokenVerifier(
            jwks, issuer="sportapp-users-management", audience="sportapp"
        )
        self.assertEqual(verifier.verify(session["access_token"]).subject, self.user_id)

        renewed = self.bff.post(
            "/v1/sessions/refresh", json={"refresh_token": session["refresh_token"]}
        )

        self.assertEqual(renewed.status_code, 200)
        self.assertEqual(verifier.verify(renewed.json()["access_token"]).subject, self.user_id)

    def test_same_generic_error_as_the_web_channel(self) -> None:
        response = self.bff.post("/v1/sessions", json={"email": EMAIL, "password": "incorrecta"})

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["code"], "invalid_credentials")
        self.assertNotIn("access_token", response.text)


if __name__ == "__main__":
    unittest.main()
