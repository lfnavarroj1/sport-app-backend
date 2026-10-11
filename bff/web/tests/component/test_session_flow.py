"""Componente HU003: BFF web → users_management → PostgreSQL (cuenta, refresh token, intentos).

El BFF usa como transporte el cliente de pruebas del servicio real; la base es
PostgreSQL real migrada (requiere `TEST_DATABASE_URL`). El access token obtenido
se valida como lo haría otro servicio: con el JWKS publicado por users_management.
"""

import unittest
import uuid

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, pool, select, update

from bff_web.main import create_app as create_bff
from bff_web.settings import BffWebSettings
from sportapp_platform.migrations import upgrade
from sportapp_platform.testing.keys import ed25519_private_key_pem
from sportapp_platform.testing.postgres import temporary_database
from sportapp_platform.tokens import AccessTokenVerifier, InvalidAccessToken
from users_management.infrastructure.persistence_write.models import (
    RefreshTokenRow,
    UserAccountRow,
)
from users_management.main import create_app as create_users
from users_management.settings import UsersManagementSettings

WRITE_HISTORY = "users_management.infrastructure.persistence_write:migrations"
POLICIES = "2026-10"
EMAIL = "ana.web@example.com"
PASSWORD = "contraseña-muy-secreta"


class SessionFlowTest(unittest.TestCase):
    def setUp(self) -> None:
        url = self.enterContext(temporary_database("bff_web_hu003"))
        upgrade(WRITE_HISTORY, url)
        users = self.enterContext(
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
        self.users = users
        settings = BffWebSettings(
            app_env="test",  # type: ignore[arg-type]
            users_management_base_url="http://users-management",  # type: ignore[arg-type]
        )
        self.bff = self.enterContext(TestClient(create_bff(settings, users_http=users)))
        self.engine = create_engine(url, poolclass=pool.NullPool)
        self.addCleanup(self.engine.dispose)
        registered = self.bff.post(
            "/v1/users",
            json={
                "full_name": "Ana Pérez",
                "email": EMAIL,
                "password": PASSWORD,
                "actor_type": "organizer",
                "accepted_policies_version": POLICIES,
            },
            headers={"Idempotency-Key": str(uuid.uuid4())},
        )
        self.assertEqual(registered.status_code, 201, registered.text)
        self.user_id = registered.json()["user_id"]

    def login(self, email: str = EMAIL, password: str = PASSWORD) -> object:
        return self.bff.post("/v1/sessions", json={"email": email, "password": password})

    def verifier(self) -> AccessTokenVerifier:
        jwks = self.users.get("/.well-known/jwks.json").json()
        return AccessTokenVerifier(jwks, issuer="sportapp-users-management", audience="sportapp")

    def refresh_tokens(self) -> int:
        with self.engine.connect() as connection:
            return connection.scalar(select(func.count()).select_from(RefreshTokenRow)) or 0

    def test_login_refresh_and_validation_by_another_service(self) -> None:
        """CA1 y CA5 de punta a punta."""
        response = self.login()

        self.assertEqual(response.status_code, 200, response.text)  # type: ignore[attr-defined]
        session = response.json()  # type: ignore[attr-defined]
        self.assertEqual(session["user"]["user_id"], self.user_id)
        self.assertEqual(session["user"]["actor_type"], "organizer")
        verified = self.verifier().verify(session["access_token"])
        self.assertEqual(verified.subject, self.user_id)

        renewed = self.bff.post(
            "/v1/sessions/refresh", json={"refresh_token": session["refresh_token"]}
        )

        self.assertEqual(renewed.status_code, 200)
        self.assertEqual(
            self.verifier().verify(renewed.json()["access_token"]).subject, self.user_id
        )
        self.assertEqual(self.refresh_tokens(), 2)

    def test_generic_rejection_and_throttling_through_the_bff(self) -> None:
        """CA2 y CA4: mismo cuerpo para correo inexistente y contraseña errada; luego 429."""
        unknown = self.login(email="nadie@example.com")
        wrong = self.login(password="otra-contraseña-incorrecta")

        for response in (unknown, wrong):
            self.assertEqual(response.status_code, 401)  # type: ignore[attr-defined]
        self.assertEqual(unknown.json()["message"], wrong.json()["message"])  # type: ignore[attr-defined]
        for _ in range(4):
            self.login(password="otra-contraseña-incorrecta")

        limited = self.login()

        self.assertEqual(limited.status_code, 429)  # type: ignore[attr-defined]
        self.assertGreater(int(limited.headers["Retry-After"]), 0)  # type: ignore[attr-defined]
        self.assertEqual(self.refresh_tokens(), 0)

    def test_disabled_account_is_rejected(self) -> None:
        """CA3."""
        with self.engine.begin() as connection:
            connection.execute(update(UserAccountRow).values(status="disabled"))

        response = self.login()

        self.assertEqual(response.status_code, 401)  # type: ignore[attr-defined]
        self.assertEqual(response.json()["code"], "invalid_credentials")  # type: ignore[attr-defined]
        self.assertEqual(self.refresh_tokens(), 0)

    def test_token_from_another_signing_key_is_rejected(self) -> None:
        """CA5: solo se aceptan identidades firmadas por la clave publicada."""
        session = self.login().json()  # type: ignore[attr-defined]
        other = AccessTokenVerifier(
            {
                "keys": [
                    {
                        "kty": "OKP",
                        "crv": "Ed25519",
                        "x": "11qYAYKxCrfVS_7TyWQHOg7hcvPapiMlrwIaaPcHURo",
                        "kid": "otra",
                    }
                ]
            },
            issuer="sportapp-users-management",
            audience="sportapp",
        )

        with self.assertRaises(InvalidAccessToken):
            other.verify(session["access_token"])


if __name__ == "__main__":
    unittest.main()
