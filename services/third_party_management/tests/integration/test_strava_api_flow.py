"""Recorrido HTTP de HU031 con `create_app` real, PostgreSQL migrado y el simulador de Strava
(ADR-042 §5-§8; CA1-CA4 y CA9).

Usuarios (JWKS) y Afiliación (capacidad `integrations.strava.connect`) se sustituyen por
transportes `httpx.MockTransport` sintéticos; los access tokens se firman con una clave
Ed25519 desechable. Strava nunca se llama: todo pasa por `StravaSimulator`. En cada prueba
se comprueba que los logs no contienen tokens, `code`, `state` ni `client_secret`.
"""

import logging
import secrets
import time
import unittest
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import load_pem_private_key
from fastapi.testclient import TestClient
from sqlalchemy import text
from support import (
    AFFILIATION_BASE_URL,
    CLIENT_ID,
    CONSENT_VERSION,
    REDIRECT_URI,
    USERS_BASE_URL,
    LogCapture,
    ThirdPartyDatabase,
    leaks,
    settings_for,
    third_party_database,
)

from sportapp_platform.testing.keys import ed25519_private_key_pem
from sportapp_platform.tokens import public_jwk
from third_party_management.infrastructure.persistence.models import (
    ExternalAccountRow,
    OAuthStateRow,
    SyncRunRow,
)
from third_party_management.main import create_app
from third_party_management.testing.strava_simulator import (
    CODE_EXCHANGE,
    DEAUTHORIZE,
    StravaSimulator,
    body_of,
    server_error,
    status,
)

CAPABILITY = "integrations.strava.connect"
ISSUER = "sportapp-users-management"
AUDIENCE = "sportapp"
SCOPE = "read,activity:read"
AUTHORIZATION = "/v1/me/integrations/strava/authorization"
CALLBACK = "/v1/integrations/strava/callback"
INTEGRATION = "/v1/me/integrations/strava"
SYNCS = "/v1/me/integrations/strava/syncs"


class StravaApiFlowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.db: ThirdPartyDatabase = self.enterContext(third_party_database("third_party_api"))
        signer = load_pem_private_key(ed25519_private_key_pem().encode("ascii"), password=None)
        assert isinstance(signer, Ed25519PrivateKey)
        self.signer = signer
        self.jwk = public_jwk(signer.public_key())
        self.capability_calls: list[str] = []
        self.strava = StravaSimulator(athlete_id=secrets.randbelow(10**8) + 1000)
        self.states: list[str] = []
        self.codes: list[str] = []

        users_http = httpx.Client(
            transport=httpx.MockTransport(self._users), base_url=USERS_BASE_URL
        )
        affiliation_http = httpx.Client(
            transport=httpx.MockTransport(self._affiliation), base_url=AFFILIATION_BASE_URL
        )
        app = create_app(
            settings_for(self.db.url),
            users_http=users_http,
            affiliation_http=affiliation_http,
            strava_http=self.strava.client(),
        )
        self.client = self.enterContext(TestClient(app))

        # La captura se instala después de crear la app: `create_service_app` reemplaza los
        # manejadores del log raíz y descartaría una captura previa.
        self.capture = LogCapture()
        root = logging.getLogger()
        previous_level = root.level
        root.addHandler(self.capture)
        root.setLevel(logging.DEBUG)
        self.addCleanup(root.setLevel, previous_level)
        self.addCleanup(root.removeHandler, self.capture)

    # ------------------------------------------------------------ dobles

    def _users(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/.well-known/jwks.json":
            return httpx.Response(200, json={"keys": [self.jwk]})
        return httpx.Response(404, json={"code": "not_found"})

    def _affiliation(self, request: httpx.Request) -> httpx.Response:
        self.capability_calls.append(request.url.path)
        return httpx.Response(
            200,
            json={"subscription_status": "ACTIVE", "capabilities": [CAPABILITY]},
        )

    def headers(self, user_id: UUID, **extra: str) -> dict[str, str]:
        issued = int(time.time())
        claims: dict[str, Any] = {
            "iss": ISSUER,
            "aud": AUDIENCE,
            "sub": str(user_id),
            "actor_type": "athlete",
            "iat": issued,
            "nbf": issued,
            "exp": issued + 900,
            "jti": str(uuid4()),
        }
        signed = jwt.encode(
            claims, self.signer, algorithm="EdDSA", headers={"kid": self.jwk["kid"]}
        )
        return {"Authorization": f"Bearer {signed}", **extra}

    # ------------------------------------------------------------ pasos

    def authorize(self, user_id: UUID) -> str:
        response = self.client.post(AUTHORIZATION, headers=self.headers(user_id))
        self.assertEqual(response.status_code, 201, response.text)
        url = response.json()["authorization_url"]
        state = parse_qs(urlsplit(url).query)["state"][0]
        self.states.append(state)
        return state

    def callback(self, **body: str) -> str:
        response = self.client.post(CALLBACK, json=body)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        result: str = response.json()["result"]
        return result

    def new_code(self) -> str:
        code = secrets.token_urlsafe(18)
        self.codes.append(code)
        return code

    def link(self, user_id: UUID) -> None:
        state = self.authorize(user_id)
        self.assertEqual(self.callback(code=self.new_code(), state=state, scope=SCOPE), "LINKED")

    def sync(self, user_id: UUID, key: UUID) -> httpx.Response:
        return self.client.post(
            SYNCS, headers=self.headers(user_id, **{"Idempotency-Key": str(key)})
        )

    def account(self, user_id: UUID) -> Any:
        return self.db.row(ExternalAccountRow, ExternalAccountRow.user_id == user_id)

    def assert_no_leaks(self) -> None:
        sensitive = [*self.strava.sensitive_values(), *self.states, *self.codes]
        self.assertTrue(sensitive)
        self.assertEqual(leaks(self.capture.text(), sensitive), [])

    # ------------------------------------------------------------ pruebas

    def test_authorization_url_uses_official_consent_and_single_use_state(self) -> None:
        """CA1: URL oficial con `scope=read,activity:read` y un `state` guardado."""
        user_id = uuid4()
        before = datetime.now(UTC)
        response = self.client.post(AUTHORIZATION, headers=self.headers(user_id))

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        document = response.json()
        url = urlsplit(document["authorization_url"])
        query = {name: values[0] for name, values in parse_qs(url.query).items()}
        self.states.append(query["state"])
        self.assertEqual(
            f"{url.scheme}://{url.netloc}{url.path}",
            "https://www.strava.com/oauth/mobile/authorize",
        )
        self.assertEqual(query["scope"], SCOPE)
        self.assertEqual(query["client_id"], CLIENT_ID)
        self.assertEqual(query["redirect_uri"], REDIRECT_URI)
        self.assertEqual(query["response_type"], "code")
        expires_at = datetime.fromisoformat(document["expires_at"].replace("Z", "+00:00"))
        self.assertLessEqual(expires_at - before, timedelta(minutes=10, seconds=5))
        self.assertGreater(expires_at - before, timedelta(minutes=9))
        stored = self.db.row(OAuthStateRow, OAuthStateRow.state == query["state"])
        self.assertEqual(stored.user_id, user_id)
        self.assertEqual(
            self.capability_calls, [f"/v1/subscriptions/by-user/{user_id}/capabilities"]
        )
        self.assert_no_leaks()

    def test_callback_links_with_encrypted_tokens_and_state_is_single_use(self) -> None:
        """CA2: `LINKED` con tokens cifrados y `consent_version`; CA3: `state` reutilizado."""
        user_id = uuid4()
        state = self.authorize(user_id)

        self.assertEqual(self.callback(code=self.new_code(), state=state, scope=SCOPE), "LINKED")
        self.assertEqual(
            self.callback(code=self.new_code(), state=state, scope=SCOPE), "INVALID_STATE"
        )

        self.assertEqual(len(self.strava.calls(CODE_EXCHANGE)), 1, "un solo intercambio")
        self.assertEqual(self.db.count(OAuthStateRow), 0)
        row = self.account(user_id)
        self.assertEqual(row.status, "ACTIVE")
        self.assertEqual(row.consent_version, CONSENT_VERSION)
        self.assertEqual(row.external_athlete_id, str(self.strava.athlete_id))
        self.assertEqual(list(row.scopes), ["read", "activity:read"])
        self.assertEqual(row.version, 1)
        access, refresh = self.strava.issued_tokens[0]
        with self.db.engine.connect() as connection:
            stored = connection.execute(
                text("SELECT * FROM external_account WHERE user_id = :user_id"),
                {"user_id": user_id},
            ).one()
        for column, value in stored._mapping.items():
            raw = bytes(value) if isinstance(value, bytes | memoryview) else str(value).encode()
            with self.subTest(column=column):
                self.assertNotIn(access.encode(), raw)
                self.assertNotIn(refresh.encode(), raw)

        current = self.client.get(INTEGRATION, headers=self.headers(user_id))
        self.assertEqual(current.status_code, 200)
        self.assertEqual(current.headers["Cache-Control"], "no-store")
        document = current.json()
        self.assertEqual(document["status"], "ACTIVE")
        self.assertEqual(document["external_athlete_id"], str(self.strava.athlete_id))
        self.assertEqual(document["scopes"], ["read", "activity:read"])
        self.assertIsNotNone(document["linked_at"])
        self.assertIsNone(document["last_sync"])
        self.assertNotIn(access, current.text)
        self.assertIn("audit.strava_link", self.capture.messages())
        self.assert_no_leaks()

    def test_denied_by_user_or_without_activity_scope(self) -> None:
        """CA3: `error=access_denied` o sin `activity:read` → `DENIED`, sin vinculación."""
        user_id = uuid4()
        denied = self.authorize(user_id)
        narrow = self.authorize(user_id)

        self.assertEqual(self.callback(state=denied, error="access_denied"), "DENIED")
        self.assertEqual(self.callback(code=self.new_code(), state=narrow, scope="read"), "DENIED")

        self.assertEqual(self.strava.calls(CODE_EXCHANGE), [])
        self.assertIsNone(self.account(user_id))
        self.assertEqual(self.db.count(OAuthStateRow), 0, "los `state` se consumieron")
        current = self.client.get(INTEGRATION, headers=self.headers(user_id)).json()
        self.assertEqual(current["status"], "NOT_LINKED")
        self.assertEqual(current["scopes"], [])
        self.assert_no_leaks()

    def test_expired_or_unknown_state_is_invalid(self) -> None:
        """CA3: un `state` vencido (más de 10 minutos) o desconocido → `INVALID_STATE`."""
        user_id = uuid4()
        state = self.authorize(user_id)
        with self.db.engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE oauth_state SET created_at = created_at - interval '11 minutes' "
                    "WHERE state = :state"
                ),
                {"state": state},
            )

        expired = self.callback(code=self.new_code(), state=state, scope=SCOPE)
        missing = self.callback(code=self.new_code(), scope=SCOPE)

        self.assertEqual(expired, "INVALID_STATE")
        self.assertEqual(missing, "INVALID_STATE")
        self.assertEqual(self.strava.calls(CODE_EXCHANGE), [])
        self.assertEqual(self.db.count(OAuthStateRow), 0, "el vencido también se consume")
        self.assertIsNone(self.account(user_id))
        self.assert_no_leaks()

    def test_failed_code_exchange_is_error(self) -> None:
        """CA3: intercambio rechazado (`400`) o caído (`503`) → `ERROR`, sin vinculación."""
        user_id = uuid4()
        self.strava.queue(CODE_EXCHANGE, status(400), server_error())

        rejected = self.callback(code=self.new_code(), state=self.authorize(user_id), scope=SCOPE)
        outage = self.callback(code=self.new_code(), state=self.authorize(user_id), scope=SCOPE)

        self.assertEqual((rejected, outage), ("ERROR", "ERROR"))
        self.assertEqual(len(self.strava.calls(CODE_EXCHANGE)), 2, "el `code` no se reintenta")
        self.assertIsNone(self.account(user_id))
        self.assertIn("strava.code_exchange_failed", self.capture.messages())
        self.assert_no_leaks()

    def test_strava_athlete_linked_to_another_user_is_error(self) -> None:
        """R: una cuenta de Strava `ACTIVE` solo puede pertenecer a un deportista."""
        owner, intruder = uuid4(), uuid4()
        self.link(owner)

        result = self.callback(code=self.new_code(), state=self.authorize(intruder), scope=SCOPE)

        self.assertEqual(result, "ERROR")
        self.assertIsNone(self.account(intruder))
        self.assertEqual(self.account(owner).status, "ACTIVE")
        self.assert_no_leaks()

    def test_sync_requests_unlink_and_sync_after_unlink(self) -> None:
        """CA4: `202` + reintento + `409 sync_in_progress`; CA9: desvincular revoca en Strava,
        borra los tokens y cancela la corrida pendiente; después `409 integration_not_linked`."""
        user_id = uuid4()
        before_link = self.sync(user_id, uuid4())
        self.assertEqual(before_link.status_code, 409)
        self.assertEqual(before_link.json()["code"], "integration_not_linked")
        self.link(user_id)
        access = self.strava.issued_tokens[0][0]
        key = uuid4()

        accepted = self.sync(user_id, key)
        replayed = self.sync(user_id, key)
        busy = self.sync(user_id, uuid4())

        self.assertEqual(accepted.status_code, 202, accepted.text)
        self.assertEqual(accepted.json()["status"], "QUEUED")
        self.assertEqual(accepted.headers["Cache-Control"], "no-store")
        self.assertNotIn("Idempotent-Replayed", accepted.headers)
        run_id = accepted.json()["sync_run_id"]
        self.assertEqual(replayed.status_code, 202)
        self.assertEqual(replayed.json(), {"sync_run_id": run_id, "status": "QUEUED"})
        self.assertEqual(replayed.headers["Idempotent-Replayed"], "true")
        self.assertEqual(busy.status_code, 409)
        self.assertEqual(busy.json()["code"], "sync_in_progress")
        self.assertEqual(self.db.count(SyncRunRow), 1)
        last_sync = self.client.get(INTEGRATION, headers=self.headers(user_id)).json()["last_sync"]
        self.assertEqual((last_sync["sync_run_id"], last_sync["status"]), (run_id, "QUEUED"))

        unlinked = self.client.delete(INTEGRATION, headers=self.headers(user_id))

        self.assertEqual(unlinked.status_code, 204)
        self.assertEqual(unlinked.headers["Cache-Control"], "no-store")
        deauthorizations = self.strava.calls(DEAUTHORIZE)
        self.assertEqual(len(deauthorizations), 1)
        self.assertEqual(body_of(deauthorizations[0])["access_token"], access)
        row = self.account(user_id)
        self.assertEqual(row.status, "REVOKED")
        self.assertIsNone(row.access_token_enc)
        self.assertIsNone(row.refresh_token_enc)
        self.assertIsNotNone(row.revoked_at)
        self.assertEqual(row.version, 2)
        run = self.db.row(SyncRunRow, SyncRunRow.sync_run_id == UUID(run_id))
        self.assertEqual((run.status, run.error_code), ("FAILED", "integration_revoked"))
        self.assertIsNotNone(run.finished_at)
        current = self.client.get(INTEGRATION, headers=self.headers(user_id)).json()
        self.assertEqual(current["status"], "REVOKED")
        self.assertEqual(current["last_sync"]["error_code"], "integration_revoked")

        after = self.sync(user_id, uuid4())
        again = self.client.delete(INTEGRATION, headers=self.headers(user_id))

        self.assertEqual(after.status_code, 409)
        self.assertEqual(after.json()["code"], "integration_not_linked")
        self.assertEqual(again.status_code, 204, "desvincular es idempotente")
        self.assertEqual(len(self.strava.calls(DEAUTHORIZE)), 1, "sin tokens no se revoca")
        self.assertIn("audit.strava_unlink", self.capture.messages())
        self.assert_no_leaks()


if __name__ == "__main__":
    unittest.main()
