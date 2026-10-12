"""HU004 contra PostgreSQL real: revocación de la sesión, idempotencia y concurrencia."""

import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from support import WriteDatabase, registration, write_database

from users_management.application.commands.login import LoginCommand
from users_management.application.commands.refresh_session import RefreshSessionCommand
from users_management.application.commands.revoke_session import (
    RevocationResult,
    RevokeSessionCommand,
)
from users_management.application.commands.sessions import SessionResult, hash_refresh_token
from users_management.domain.authentication.errors import InvalidRefreshToken
from users_management.infrastructure.persistence_write.models import RefreshTokenRow

PASSWORD = "contraseña-muy-secreta"
CONCURRENCY = 8


def run_concurrently(task: Any, times: int = CONCURRENCY) -> list[Any]:
    barrier = threading.Barrier(times)

    def synchronized(_: int) -> Any:
        barrier.wait()
        try:
            return task()
        except Exception as error:
            return error

    with ThreadPoolExecutor(max_workers=times) as pool:
        return list(pool.map(synchronized, range(times)))


class SessionRevocationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.db: WriteDatabase = self.enterContext(write_database("users_hu004"))
        self.email = "ana.cierre@example.com"
        self.db.register_user()(registration(email=self.email, password=PASSWORD))

    def login(self) -> SessionResult:
        return self.db.login()(
            LoginCommand(email=self.email, password=PASSWORD, client_ip="203.0.113.7")
        )

    def revoke(self, token: str) -> RevocationResult:
        return self.db.revoke_session()(RevokeSessionCommand(refresh_token=token))

    def refresh(self, token: str) -> SessionResult:
        return self.db.refresh_session()(RefreshSessionCommand(refresh_token=token))

    def family_rows(self, token: str) -> list[RefreshTokenRow]:
        with self.db.sessions() as session:
            family = session.scalar(
                select(RefreshTokenRow.family_id).where(
                    RefreshTokenRow.token_hash == hash_refresh_token(token)
                )
            )
            return list(
                session.scalars(select(RefreshTokenRow).where(RefreshTokenRow.family_id == family))
            )

    def test_revocation_closes_only_that_session(self) -> None:
        """CA1 y CA3 con persistencia real."""
        closed, other = self.login(), self.login()
        rotated = self.refresh(closed.refresh_token)

        self.assertEqual(self.revoke(rotated.refresh_token), RevocationResult.REVOKED)

        self.assertTrue(all(row.revoked_at for row in self.family_rows(closed.refresh_token)))
        with self.assertRaises(InvalidRefreshToken):
            self.refresh(rotated.refresh_token)
        self.assertTrue(self.refresh(other.refresh_token).refresh_token)

    def test_concurrent_repeated_revocations_are_idempotent(self) -> None:
        """CA2 y CA7: 8 cierres simultáneos con la misma credencial."""
        session = self.login()

        results = run_concurrently(lambda: self.revoke(session.refresh_token))

        self.assertEqual([r for r in results if isinstance(r, Exception)], [])
        self.assertEqual(results.count(RevocationResult.REVOKED), 1)
        self.assertEqual(results.count(RevocationResult.ALREADY_REVOKED), CONCURRENCY - 1)
        self.assertEqual(self.revoke("t" * 43), RevocationResult.UNKNOWN)

    def test_revocation_waits_for_an_in_flight_renewal(self) -> None:
        """Una renovación en curso con el token vigente bloquea su fila y crea un sucesor; la
        revocación hecha con el token anterior espera y también revoca a ese sucesor."""
        first = self.login()
        current = self.refresh(first.refresh_token)
        done = threading.Event()

        def revoke_with_previous_token() -> None:
            self.revoke(first.refresh_token)
            done.set()

        with self.db.sessions() as session, session.begin():
            row = session.scalars(
                select(RefreshTokenRow)
                .where(RefreshTokenRow.token_hash == hash_refresh_token(current.refresh_token))
                .with_for_update()
            ).one()
            worker = threading.Thread(target=revoke_with_previous_token)
            worker.start()
            self.assertFalse(done.wait(1.0), "la revocación no esperó a la renovación en curso")
            now = datetime.now(UTC)
            successor_id = uuid4()
            row.used_at, row.replaced_by = now, successor_id
            session.add(
                RefreshTokenRow(
                    token_id=successor_id,
                    family_id=row.family_id,
                    user_id=row.user_id,
                    token_hash=hash_refresh_token("sucesor-" + uuid4().hex),
                    issued_at=now,
                    expires_at=now + timedelta(days=7),
                )
            )
        worker.join(timeout=10)

        self.assertTrue(done.is_set())
        rows = self.family_rows(first.refresh_token)
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(row.revoked_at is not None for row in rows), "sucesor sin revocar")

    def test_concurrent_refresh_and_revoke_never_leave_a_live_session(self) -> None:
        """Con el mismo token, o la renovación falla o su sucesor queda revocado."""
        for _ in range(5):
            token = self.login().refresh_token
            barrier = threading.Barrier(2)
            outcomes: dict[str, Any] = {}

            def run(
                name: str,
                operation: Any,
                token: str = token,
                barrier: threading.Barrier = barrier,
                outcomes: dict[str, Any] = outcomes,
            ) -> None:
                barrier.wait()
                try:
                    outcomes[name] = operation(token)
                except Exception as error:
                    outcomes[name] = error

            workers = [
                threading.Thread(target=run, args=("refresh", self.refresh)),
                threading.Thread(target=run, args=("revoke", self.revoke)),
            ]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(timeout=10)

            self.assertEqual(outcomes["revoke"], RevocationResult.REVOKED)
            refreshed = outcomes["refresh"]
            self.assertTrue(
                isinstance(refreshed, (SessionResult, InvalidRefreshToken)), repr(refreshed)
            )
            live = [row for row in self.family_rows(token) if row.revoked_at is None]
            self.assertEqual(live, [], f"sesión activa tras el cierre: {outcomes}")


if __name__ == "__main__":
    unittest.main()
