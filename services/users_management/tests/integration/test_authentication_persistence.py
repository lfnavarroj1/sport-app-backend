"""HU003 contra PostgreSQL real: sesión, refresh tokens y control de abuso, con concurrencia."""

import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from support import TOKENS, WriteDatabase, registration, write_database

from sportapp_platform.tokens import AccessTokenVerifier
from users_management.application.commands.login import LoginCommand
from users_management.application.commands.refresh_session import RefreshSessionCommand
from users_management.application.commands.sessions import SessionResult, hash_refresh_token
from users_management.domain.authentication.errors import (
    InvalidCredentials,
    InvalidRefreshToken,
)
from users_management.domain.authentication.model import LoginThrottlePolicy
from users_management.infrastructure.persistence_write.models import (
    LoginFailureWindowRow,
    RefreshTokenRow,
)

PASSWORD = "contraseña-muy-secreta"
IP = "203.0.113.7"
CONCURRENCY = 8


class AuthenticationPersistenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.db: WriteDatabase = self.enterContext(write_database("users_hu003"))
        self.email = "ana.sesion@example.com"
        self.user = self.db.register_user()(registration(email=self.email, password=PASSWORD))

    def login(
        self, password: str = PASSWORD, throttle: LoginThrottlePolicy | None = None
    ) -> SessionResult:
        login = self.db.login(throttle) if throttle else self.db.login()
        return login(LoginCommand(email=self.email, password=password, client_ip=IP))

    def test_login_against_real_hash_issues_verifiable_tokens_and_stores_only_the_hash(
        self,
    ) -> None:
        """CA1 y CA5 con Argon2id y PostgreSQL reales."""
        session = self.login()

        verifier = AccessTokenVerifier(
            TOKENS.jwks(), issuer="sportapp-users-management", audience="sportapp"
        )
        self.assertEqual(verifier.verify(session.access_token).subject, str(self.user.user_id))
        with self.db.sessions() as db_session:
            [row] = db_session.scalars(select(RefreshTokenRow)).all()
        self.assertEqual(row.user_id, self.user.user_id)
        self.assertEqual(row.token_hash, hash_refresh_token(session.refresh_token))
        self.assertIsNone(row.used_at)
        self.assertIsNone(row.revoked_at)

    def test_wrong_password_records_a_hashed_failure_and_no_token(self) -> None:
        """CA2."""
        with self.assertRaises(InvalidCredentials):
            self.login(password="otra-contraseña-incorrecta")

        with self.db.sessions() as db_session:
            [window] = db_session.scalars(select(LoginFailureWindowRow)).all()
        self.assertEqual(window.failures, 1)
        self.assertNotIn("ana", window.throttle_key)
        self.assertEqual(self.db.count(RefreshTokenRow), 0)

    def test_disabled_status_is_allowed_by_the_schema_and_blocks_login(self) -> None:
        """CA3."""
        self.db.disable(self.user.user_id)

        with self.assertRaises(InvalidCredentials):
            self.login()

        self.assertEqual(self.db.count(RefreshTokenRow), 0)

    def test_concurrent_failures_are_all_counted(self) -> None:
        """CA4: el contador no pierde incrementos bajo concurrencia."""
        now = datetime.now(UTC)

        def fail(_: int) -> None:
            with self.db.authentication() as unit_of_work:
                unit_of_work.record_failure("k" * 64, now, timedelta(minutes=15))
                unit_of_work.commit()

        with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
            list(pool.map(fail, range(CONCURRENCY)))

        with self.db.authentication() as unit_of_work:
            window = unit_of_work.find_failure_window("k" * 64)
        assert window is not None
        self.assertEqual(window.failures, CONCURRENCY)

    def test_expired_window_restarts_the_count(self) -> None:
        start = datetime.now(UTC) - timedelta(minutes=20)
        with self.db.authentication() as unit_of_work:
            unit_of_work.record_failure("w" * 64, start, timedelta(minutes=15))
            unit_of_work.record_failure("w" * 64, start, timedelta(minutes=15))
            restarted = unit_of_work.record_failure(
                "w" * 64, start + timedelta(minutes=20), timedelta(minutes=15)
            )
            unit_of_work.commit()

        self.assertEqual(restarted.failures, 1)
        self.assertEqual(restarted.started_at, start + timedelta(minutes=20))

    def test_successful_login_clears_the_failures(self) -> None:
        with self.assertRaises(InvalidCredentials):
            self.login(password="otra-contraseña-incorrecta")

        self.login()

        with self.db.sessions() as db_session:
            self.assertEqual(db_session.scalars(select(LoginFailureWindowRow)).all(), [])

    def test_refresh_rotation_is_persisted(self) -> None:
        session = self.login()

        renewed = self.db.refresh_session()(
            RefreshSessionCommand(refresh_token=session.refresh_token)
        )

        with self.db.sessions() as db_session:
            old = db_session.scalars(
                select(RefreshTokenRow).where(
                    RefreshTokenRow.token_hash == hash_refresh_token(session.refresh_token)
                )
            ).one()
            new = db_session.scalars(
                select(RefreshTokenRow).where(
                    RefreshTokenRow.token_hash == hash_refresh_token(renewed.refresh_token)
                )
            ).one()
        self.assertIsNotNone(old.used_at)
        self.assertEqual(old.replaced_by, new.token_id)
        self.assertEqual(new.family_id, old.family_id)

    def test_concurrent_refreshes_with_the_same_token_cannot_both_succeed(self) -> None:
        """La fila bloqueada serializa: una renueva y las demás detectan reutilización."""
        session = self.login()
        refresh = self.db.refresh_session()

        def attempt(_: int) -> object:
            try:
                return refresh(RefreshSessionCommand(refresh_token=session.refresh_token))
            except InvalidRefreshToken as error:
                return error

        with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
            outcomes = list(pool.map(attempt, range(CONCURRENCY)))

        succeeded = [o for o in outcomes if isinstance(o, SessionResult)]
        self.assertEqual(len(succeeded), 1)
        with self.db.sessions() as db_session:
            rows = db_session.scalars(select(RefreshTokenRow)).all()
        self.assertTrue(all(row.revoked_at is not None for row in rows), "familia revocada")


if __name__ == "__main__":
    unittest.main()
