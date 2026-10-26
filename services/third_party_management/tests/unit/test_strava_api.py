"""Rutas HTTP de HU031 (ADR-042) con `TestClient`, los casos de uso reales y dobles en memoria:
identidad (`401`/`403`/`503`), capacidad (`403`/`503`/`504`), Strava no configurado
(`503`), CA1-CA3 (autorización y callback), CA4 (sincronización `202` idempotente), CA9
(desvinculación `204`) y estado. Toda respuesta lleva `Cache-Control: no-store` y ninguna
respuesta ni log contiene tokens, `code` ni `state`."""

import logging
import secrets
import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

from fakes import (
    CAPABILITY,
    CLIENT_ID,
    CONSENT_VERSION,
    NOW,
    REDIRECT_URI,
    VERIFIER,
    FakeCapabilities,
    FixedClock,
    InMemoryStore,
    InMemoryUnitOfWork,
    linked_account,
    strava_context,
    token,
)
from fastapi.testclient import TestClient
from httpx import Response
from leak_checks import leaks

from sportapp_platform.app import create_service_app
from sportapp_platform.config import ServiceSettings
from sportapp_platform.tokens import JwksUnavailable, VerifiedAccessToken
from third_party_management.api.strava import CALLBACK_FIELD_MAX, build_strava_router
from third_party_management.application.connections import GetIntegrationStatus, UnlinkStrava
from third_party_management.application.oauth import CompleteCallback, StartAuthorization
from third_party_management.application.ports import StravaContext
from third_party_management.application.syncs import RequestSync
from third_party_management.domain.connections import AccountStatus
from third_party_management.domain.synchronization import SyncErrorCode, SyncRun, SyncStatus
from third_party_management.testing.strava_simulator import (
    CODE_EXCHANGE,
    DEAUTHORIZE,
    StravaSimulator,
    status,
)

AUTHORIZATION = "/v1/me/integrations/strava/authorization"
CALLBACK = "/v1/integrations/strava/callback"
INTEGRATION = "/v1/me/integrations/strava"
SYNCS = "/v1/me/integrations/strava/syncs"
SCOPE = "read,activity:read"
ATHLETE_ID = 777

# (método, ruta) de las cuatro operaciones autenticadas.
PROTECTED = (
    ("POST", AUTHORIZATION),
    ("GET", INTEGRATION),
    ("DELETE", INTEGRATION),
    ("POST", SYNCS),
)


def utc(value: Any) -> str:
    return value.isoformat().replace("+00:00", "Z")


class _Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self.store = InMemoryStore()
        self.clock = FixedClock()
        self.user_id = uuid4()
        self.capabilities = FakeCapabilities({self.user_id: {CAPABILITY}})
        self.strava = StravaSimulator(athlete_id=ATHLETE_ID)
        self.context = strava_context(self.strava)
        self.jwks_down = False
        self.client = self.build(self.context)
        self.auth = self.bearer(self.user_id, "athlete")

    def build(self, strava: StravaContext | None) -> TestClient:
        def verify(raw: str) -> VerifiedAccessToken:
            if self.jwks_down:
                raise JwksUnavailable()
            return VERIFIER.verify(raw)

        def unit_of_work() -> InMemoryUnitOfWork:
            return InMemoryUnitOfWork(self.store)

        capabilities, clock = self.capabilities, self.clock
        app = create_service_app(
            service_name="third-party-management",
            title="t",
            api_version="t",
            settings=ServiceSettings(app_env="test"),
            routers=[
                build_strava_router(
                    verify_token=verify,
                    start_authorization=StartAuthorization(
                        unit_of_work=unit_of_work,
                        capabilities=capabilities,
                        strava=strava,
                        clock=clock,
                    ),
                    complete_callback=CompleteCallback(
                        unit_of_work=unit_of_work, strava=strava, clock=clock
                    ),
                    get_status=GetIntegrationStatus(
                        unit_of_work=unit_of_work, capabilities=capabilities
                    ),
                    unlink=UnlinkStrava(
                        unit_of_work=unit_of_work,
                        capabilities=capabilities,
                        strava=strava,
                        clock=clock,
                    ),
                    request_sync=RequestSync(
                        unit_of_work=unit_of_work,
                        capabilities=capabilities,
                        strava=strava,
                        clock=clock,
                    ),
                )
            ],
        )
        return TestClient(app)

    @staticmethod
    def bearer(user_id: UUID, actor_type: str | None) -> dict[str, str]:
        return {"Authorization": f"Bearer {token(user_id, actor_type)}"}

    def call(
        self,
        method: str,
        path: str,
        headers: dict[str, str] | None = None,
        client: TestClient | None = None,
        **kwargs: Any,
    ) -> Response:
        response = (client or self.client).request(method, path, headers=headers, **kwargs)
        self.assertEqual(response.headers.get("Cache-Control"), "no-store", f"{method} {path}")
        return response

    def protected(
        self, method: str, path: str, headers: dict[str, str] | None = None, **kwargs: Any
    ) -> Response:
        if path == SYNCS:
            headers = {"Idempotency-Key": str(uuid4()), **(headers or {})}
        return self.call(method, path, headers, **kwargs)

    def assert_error(self, response: Response, status_code: int, code: str) -> None:
        self.assertEqual(response.status_code, status_code, response.text)
        body = response.json()
        self.assertEqual(body["code"], code)
        self.assertIn("correlation_id", body)

    def start(self) -> Response:
        return self.call("POST", AUTHORIZATION, self.auth)

    def state(self) -> str:
        return parse_qs(urlsplit(self.start().json()["authorization_url"]).query)["state"][0]

    def callback(self, **values: str | None) -> Response:
        return self.call("POST", CALLBACK, json=values)

    def link(self) -> None:
        response = self.callback(code="codigo-sintetico", state=self.state(), scope=SCOPE)
        self.assertEqual(response.json(), {"result": "LINKED"})

    def sync(self, key: str | None, headers: dict[str, str] | None = None) -> Response:
        sent = dict(headers if headers is not None else self.auth)
        if key is not None:
            sent["Idempotency-Key"] = key
        return self.call("POST", SYNCS, sent)


class AuthenticationTest(_Fixture):
    def test_missing_or_invalid_token_is_401_with_bearer_challenge(self) -> None:
        cases = {
            "sin cabecera": None,
            "otro esquema": "Basic dXN1YXJpbw==",
            "bearer vacío": "Bearer ",
            "firma inválida": f"Bearer {secrets.token_urlsafe(32)}",
        }
        for method, path in PROTECTED:
            for name, value in cases.items():
                with self.subTest(route=f"{method} {path}", case=name):
                    headers = {} if value is None else {"Authorization": value}
                    response = self.protected(method, path, headers)

                    self.assert_error(response, 401, "invalid_access_token")
                    self.assertEqual(response.headers["WWW-Authenticate"], "Bearer")
        self.assertEqual(self.capabilities.calls, [], "sin identidad no se consulta Afiliación")

    def test_jwks_unavailable_is_503(self) -> None:
        self.jwks_down = True
        for method, path in PROTECTED:
            with self.subTest(route=f"{method} {path}"):
                with self.assertLogs("sportapp.integrations", level="WARNING") as logs:
                    response = self.protected(method, path, self.auth)

                self.assert_error(response, 503, "upstream_unavailable")
                self.assertNotIn("WWW-Authenticate", response.headers)
                self.assertEqual(logs.records[0].getMessage(), "integrations.jwks_unavailable")

    def test_actor_other_than_athlete_or_missing_is_403_permission_required(self) -> None:
        for actor in ("organizer", "service_provider", None):
            headers = self.bearer(self.user_id, actor)
            for method, path in PROTECTED:
                with self.subTest(actor=actor, route=f"{method} {path}"):
                    response = self.protected(method, path, headers)

                    self.assert_error(response, 403, "permission_required")
        self.assertEqual(self.capabilities.calls, [])

    def test_athlete_without_capability_is_403_capability_required(self) -> None:
        headers = self.bearer(uuid4(), "athlete")
        for method, path in PROTECTED:
            with self.subTest(route=f"{method} {path}"):
                response = self.protected(method, path, headers)

                self.assert_error(response, 403, "capability_required")
        self.assertTrue(all(name == CAPABILITY for _, name in self.capabilities.calls))

    def test_affiliation_unavailable_is_503_and_timeout_is_504(self) -> None:
        for attribute, status_code, code in (
            ("unavailable", 503, "upstream_unavailable"),
            ("slow", 504, "upstream_timeout"),
        ):
            setattr(self.capabilities, attribute, True)
            for method, path in PROTECTED:
                with self.subTest(case=attribute, route=f"{method} {path}"):
                    response = self.protected(method, path, self.auth)

                    self.assert_error(response, status_code, code)
            setattr(self.capabilities, attribute, False)


class StravaNotConfiguredTest(_Fixture):
    def setUp(self) -> None:
        super().setUp()
        self.unconfigured = self.build(None)

    def test_operations_that_need_strava_answer_503(self) -> None:
        start = self.call("POST", AUTHORIZATION, self.auth, client=self.unconfigured)
        callback = self.call(
            "POST",
            CALLBACK,
            client=self.unconfigured,
            json={"code": "codigo-sintetico", "state": "s", "scope": SCOPE},
        )
        self.store.accounts[self.user_id] = self.linked()
        sync = self.call(
            "POST",
            SYNCS,
            {**self.auth, "Idempotency-Key": str(uuid4())},
            client=self.unconfigured,
        )

        for response in (start, callback, sync):
            self.assert_error(response, 503, "upstream_unavailable")
        self.assertEqual(self.store.runs, {})

    def test_status_and_unlink_work_without_configuration(self) -> None:
        self.store.accounts[self.user_id] = self.linked()

        before = self.call("GET", INTEGRATION, self.auth, client=self.unconfigured)
        with self.assertLogs("sportapp.integrations", level="WARNING") as logs:
            unlink = self.call("DELETE", INTEGRATION, self.auth, client=self.unconfigured)

        self.assertEqual(before.json()["status"], "ACTIVE")
        self.assertEqual(unlink.status_code, 204)
        self.assertEqual(logs.records[0].getMessage(), "strava.deauthorize_skipped")
        self.assertIs(self.store.accounts[self.user_id].status, AccountStatus.REVOKED)
        self.assertEqual(self.strava.calls(DEAUTHORIZE), [])

    def linked(self) -> Any:
        access, refresh = self.strava.issue_tokens()
        return linked_account(
            self.context,
            self.user_id,
            access=access,
            refresh=refresh,
            expires_at=NOW + timedelta(hours=6),
        )


class AuthorizationTest(_Fixture):
    def test_201_returns_official_consent_url_with_single_use_state(self) -> None:
        """CA1."""
        response = self.start()

        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        self.assertEqual(set(body), {"authorization_url", "expires_at"})
        self.assertEqual(body["expires_at"], utc(NOW + timedelta(minutes=10)))
        url = urlsplit(body["authorization_url"])
        query = parse_qs(url.query)
        self.assertEqual(f"{url.scheme}://{url.netloc}{url.path}", self.authorize_url())
        self.assertEqual(query["scope"], [SCOPE])
        self.assertEqual(query["client_id"], [CLIENT_ID])
        self.assertEqual(query["redirect_uri"], [REDIRECT_URI])
        self.assertEqual(query["response_type"], ["code"])
        [state] = query["state"]
        self.assertIn(state, self.store.states)
        self.assertEqual(self.store.states[state].user_id, self.user_id)

    def test_each_request_issues_a_different_state(self) -> None:
        first, second = self.state(), self.state()

        self.assertNotEqual(first, second)

    @staticmethod
    def authorize_url() -> str:
        return "https://www.strava.com/oauth/mobile/authorize"


class CallbackTest(_Fixture):
    def test_linked_stores_encrypted_tokens_and_consent_version(self) -> None:
        """CA2."""
        response = self.callback(code="codigo-sintetico", state=self.state(), scope=SCOPE)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"result": "LINKED"})
        account = self.store.accounts[self.user_id]
        self.assertIs(account.status, AccountStatus.ACTIVE)
        self.assertEqual(account.external_athlete_id, str(ATHLETE_ID))
        self.assertEqual(account.consent_version, CONSENT_VERSION)
        self.assertIsNotNone(account.tokens)

    def test_denied_invalid_state_and_error(self) -> None:
        """CA3: rechazo, scope insuficiente, `state` ajeno o reutilizado e intercambio
        fallido no vinculan nada."""
        reused = self.state()
        self.callback(error="access_denied", state=reused)
        self.strava.queue(CODE_EXCHANGE, status(400))
        cases = [
            ({"error": "access_denied", "state": self.state()}, "DENIED"),
            ({"code": "c", "state": self.state(), "scope": "read"}, "DENIED"),
            ({"state": self.state(), "scope": SCOPE}, "DENIED"),
            ({"code": "c", "state": secrets.token_urlsafe(32), "scope": SCOPE}, "INVALID_STATE"),
            ({"code": "c", "state": reused, "scope": SCOPE}, "INVALID_STATE"),
            ({"code": "c", "scope": SCOPE}, "INVALID_STATE"),
            ({"code": "c", "state": self.state(), "scope": SCOPE}, "ERROR"),
        ]
        for values, expected in cases:
            with self.subTest(values=values):
                response = self.callback(**values)

                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json(), {"result": expected})
        self.assertEqual(self.store.accounts, {})

    def test_callback_needs_no_sportapp_token(self) -> None:
        self.jwks_down = True

        response = self.callback(code="codigo-sintetico", state=self.state_offline(), scope=SCOPE)

        self.assertEqual(response.json(), {"result": "LINKED"})

    def state_offline(self) -> str:
        self.jwks_down = False
        state = self.state()
        self.jwks_down = True
        return state

    def test_body_shape_is_validated(self) -> None:
        long_value = "x" * (CALLBACK_FIELD_MAX + 1)
        cases = {
            "campo de más": ({"code": "c", "state": "s", "athlete_id": "1"}, "body.athlete_id"),
            "code largo": ({"code": long_value, "state": "s"}, "body.code"),
            "state largo": ({"code": "c", "state": long_value}, "body.state"),
            "scope largo": ({"scope": long_value}, "body.scope"),
            "error largo": ({"error": long_value}, "body.error"),
        }
        for name, (body, field) in cases.items():
            with self.subTest(case=name):
                response = self.client.post(CALLBACK, json=body)

                self.assert_error(response, 422, "validation_error")
                self.assertEqual(
                    [detail["field"] for detail in response.json()["details"]], [field]
                )
                self.assertNotIn(long_value, response.text)
        self.assertEqual(self.store.accounts, {})

    def test_field_at_the_limit_is_accepted(self) -> None:
        response = self.callback(state="s" * CALLBACK_FIELD_MAX)

        self.assertEqual(response.json(), {"result": "INVALID_STATE"})


class StatusTest(_Fixture):
    def test_not_linked(self) -> None:
        response = self.call("GET", INTEGRATION, self.auth)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "status": "NOT_LINKED",
                "external_athlete_id": None,
                "linked_at": None,
                "scopes": [],
                "last_sync": None,
            },
        )

    def test_active_with_last_sync(self) -> None:
        self.link()
        run = SyncRun.queue(self.user_id, NOW - timedelta(hours=1), None).finish(
            SyncErrorCode.RATE_LIMITED, 3, NOW - timedelta(minutes=50)
        )
        older = SyncRun.queue(self.user_id, NOW - timedelta(days=1), None)
        self.store.runs.update({run.sync_run_id: run, older.sync_run_id: older})

        body = self.call("GET", INTEGRATION, self.auth).json()

        self.assertEqual(body["status"], "ACTIVE")
        self.assertEqual(body["external_athlete_id"], str(ATHLETE_ID))
        self.assertEqual(body["linked_at"], utc(NOW))
        self.assertEqual(body["scopes"], ["read", "activity:read"])
        self.assertEqual(
            body["last_sync"],
            {
                "sync_run_id": str(run.sync_run_id),
                "status": "RATE_LIMITED",
                "requested_at": utc(NOW - timedelta(hours=1)),
                "finished_at": utc(NOW - timedelta(minutes=50)),
                "imported_count": 3,
                "error_code": "rate_limited",
            },
        )

    def test_revoked_and_expired(self) -> None:
        self.link()
        self.call("DELETE", INTEGRATION, self.auth)

        revoked = self.call("GET", INTEGRATION, self.auth).json()

        self.store.accounts[self.user_id] = self.store.accounts[self.user_id].expire(NOW)
        expired = self.call("GET", INTEGRATION, self.auth).json()

        self.assertEqual(revoked["status"], "REVOKED")
        self.assertEqual(expired["status"], "EXPIRED")
        self.assertEqual(expired["external_athlete_id"], str(ATHLETE_ID))

    def test_other_athletes_link_is_not_visible(self) -> None:
        self.link()
        other = uuid4()
        self.capabilities.granted[other] = {CAPABILITY}

        body = self.call("GET", INTEGRATION, self.bearer(other, "athlete")).json()

        self.assertEqual(body["status"], "NOT_LINKED")


class UnlinkTest(_Fixture):
    def test_204_without_link(self) -> None:
        response = self.call("DELETE", INTEGRATION, self.auth)

        self.assertEqual(response.status_code, 204)
        self.assertEqual(response.content, b"")

    def test_204_revokes_and_cancels_pending_runs(self) -> None:
        """CA9."""
        self.link()
        accepted = self.sync(str(uuid4())).json()

        first = self.call("DELETE", INTEGRATION, self.auth)
        again = self.call("DELETE", INTEGRATION, self.auth)

        self.assertEqual((first.status_code, again.status_code), (204, 204))
        account = self.store.accounts[self.user_id]
        self.assertIs(account.status, AccountStatus.REVOKED)
        self.assertIsNone(account.tokens)
        run = self.store.runs[UUID(accepted["sync_run_id"])]
        self.assertIs(run.status, SyncStatus.FAILED)
        self.assertIs(run.error_code, SyncErrorCode.INTEGRATION_REVOKED)
        self.assertEqual(len(self.strava.calls(DEAUTHORIZE)), 1)


class SyncTest(_Fixture):
    def test_202_and_replay_with_the_same_key(self) -> None:
        """CA4: misma clave → misma corrida con `Idempotent-Replayed: true`."""
        self.link()
        key = str(uuid4())

        first = self.sync(key)
        replay = self.sync(key)

        self.assertEqual(first.status_code, 202, first.text)
        self.assertNotIn("Idempotent-Replayed", first.headers)
        body = first.json()
        self.assertEqual(body["status"], "QUEUED")
        self.assertEqual(replay.status_code, 202)
        self.assertEqual(replay.headers["Idempotent-Replayed"], "true")
        self.assertEqual(replay.json(), body)
        self.assertEqual(len(self.store.runs), 1)
        run = self.store.runs[UUID(body["sync_run_id"])]
        self.assertIsNotNone(run.correlation_id, "la corrida conserva la correlación")

    def test_replay_returns_the_current_state_of_the_run(self) -> None:
        self.link()
        key = str(uuid4())
        run_id = UUID(self.sync(key).json()["sync_run_id"])
        self.store.runs[run_id] = self.store.runs[run_id].finish(None, 2, NOW)

        replay = self.sync(key).json()

        self.assertEqual(replay, {"sync_run_id": str(run_id), "status": "SUCCEEDED"})

    def test_409_integration_not_linked_and_sync_in_progress(self) -> None:
        not_linked = self.sync(str(uuid4()))
        self.link()
        self.sync(str(uuid4()))
        in_progress = self.sync(str(uuid4()))

        self.assert_error(not_linked, 409, "integration_not_linked")
        self.assert_error(in_progress, 409, "sync_in_progress")
        self.assertEqual(len(self.store.runs), 1)

    def test_422_idempotency_key(self) -> None:
        for raw, issue in ((None, "required"), ("  ", "required"), ("abc", "invalid_uuid")):
            with self.subTest(raw=raw):
                response = self.sync(raw)

                self.assert_error(response, 422, "validation_error")
                self.assertEqual(
                    response.json()["details"], [{"field": "Idempotency-Key", "issue": issue}]
                )
        self.assertEqual(self.store.runs, {})

    def test_actor_is_checked_before_the_key(self) -> None:
        response = self.sync(None, self.bearer(self.user_id, "organizer"))

        self.assert_error(response, 403, "permission_required")


class NoLeakTest(_Fixture):
    @contextmanager
    def captured(self) -> Iterator[list[logging.LogRecord]]:
        with self.assertLogs("sportapp", level="DEBUG") as logs:
            yield logs.records

    def test_responses_and_logs_never_carry_tokens_code_or_state(self) -> None:
        code = f"codigo-{secrets.token_urlsafe(12)}"
        responses: list[Response] = []
        with self.captured() as records:
            started = self.start()
            state = parse_qs(urlsplit(started.json()["authorization_url"]).query)["state"][0]
            responses.append(self.callback(code=code, state=state, scope=SCOPE))
            responses.append(self.callback(code=code, state=state, scope=SCOPE))
            responses.append(self.call("GET", INTEGRATION, self.auth))
            responses.append(self.sync(str(uuid4())))
            responses.append(self.call("DELETE", INTEGRATION, self.auth))

        sensitive = [*self.strava.sensitive_values(), code, state]
        self.assertIn(code, self.strava.sensitive_values(), "el simulador recibió el code")
        for response in responses:
            text = response.text + str(dict(response.headers))
            with self.subTest(response=response.request.url.path):
                self.assertEqual([value for value in sensitive if value in text], [])
        for record in records:
            fields = {"message": record.getMessage(), **getattr(record, "fields", {})}
            with self.subTest(log=record.getMessage()):
                self.assertEqual(leaks(fields, sensitive), [])
        audits = {record.getMessage() for record in records if record.name == "sportapp.audit"}
        self.assertEqual(
            audits, {"audit.strava_link", "audit.strava_sync_requested", "audit.strava_unlink"}
        )


if __name__ == "__main__":
    unittest.main()
