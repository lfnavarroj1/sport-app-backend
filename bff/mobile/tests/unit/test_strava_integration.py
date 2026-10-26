"""BFF móvil, HU031: vinculación y sincronización con Strava (ADR-042).

Operaciones del deportista: el borde exige `athlete` y la capacidad vigente
`integrations.strava.connect`; lo demás llega de Terceros sin alterar (cuerpo, códigos,
`Idempotent-Replayed`); la autorización y el estado salen con `Cache-Control: no-store`.
Callback público: reenvía los parámetros en JSON y siempre redirige (`302`) al enlace profundo;
cualquier fallo de Terceros es `ERROR`. Ni `code`, ni `state`, ni `scope` llegan a los logs.
El router se monta solo, con los mismos clientes que `create_app`, para probar el borde.
"""

import json
import logging
import secrets
import unittest
import uuid
from typing import Any

import httpx
from fastapi.testclient import TestClient
from test_capabilities import Upstream, token

from bff_mobile.api.strava_integration import build_strava_integration_router
from bff_mobile.clients.affiliation_management import AffiliationManagementClient
from bff_mobile.clients.third_party_management import ThirdPartyManagementClient
from bff_mobile.clients.users_management import UsersManagementClient
from bff_mobile.settings import BffMobileSettings
from sportapp_platform.app import create_service_app
from sportapp_platform.tokens import CachedJwksVerifier

STRAVA = "integrations.strava.connect"
INTEGRATION_PATH = "/v1/me/integrations/strava"
AUTHORIZATION_PATH = f"{INTEGRATION_PATH}/authorization"
SYNCS_PATH = f"{INTEGRATION_PATH}/syncs"
CALLBACK_PATH = "/v1/integrations/strava/callback"
DEEP_LINK = "sportapp://integrations/strava?result="
SYNC_RUN_ID = str(uuid.uuid4())
NOT_LINKED = {
    "status": "NOT_LINKED",
    "external_athlete_id": None,
    "linked_at": None,
    "scopes": [],
    "last_sync": None,
}
LINKED = {
    "status": "ACTIVE",
    "external_athlete_id": "12345",
    "linked_at": "2026-10-24T12:00:00Z",
    "scopes": ["read", "activity:read"],
    "last_sync": {
        "sync_run_id": SYNC_RUN_ID,
        "status": "SUCCEEDED",
        "requested_at": "2026-10-24T12:05:00Z",
        "finished_at": "2026-10-24T12:05:30Z",
        "imported_count": 3,
        "error_code": "invalid_activity",
    },
}
QUEUED = {"sync_run_id": SYNC_RUN_ID, "status": "QUEUED"}
OPERATIONS = ("authorize", "status", "unlink", "sync")
RESULTS = ("LINKED", "DENIED", "INVALID_STATE", "ERROR")


def error(code: str, details: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {"code": code, "message": "m", "correlation_id": "c", "details": details or []}


def authorization_reply() -> dict[str, Any]:
    """URL sintética con un `state` aleatorio (nunca credenciales reales de Strava)."""
    state = secrets.token_urlsafe(32)
    return {
        "authorization_url": "https://www.strava.com/oauth/mobile/authorize?client_id=1"
        f"&response_type=code&scope=read%2Cactivity%3Aread&state={state}",
        "expires_at": "2026-10-24T12:10:00Z",
    }


class SlowableUpstream(Upstream):
    """Afiliación que además puede agotar el tiempo de espera."""

    slow = False

    def affiliation(self, request: httpx.Request) -> httpx.Response:
        if self.slow:
            raise httpx.ReadTimeout("afiliación lenta")
        return super().affiliation(request)


class RecordedLogs(logging.Handler):
    """Mensaje y campos de cada registro, sin la latencia (sus cifras son aleatorias)."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def text(self) -> str:
        lines = []
        for record in self.records:
            fields = dict(getattr(record, "fields", {}) or {})
            fields.pop("duration_ms", None)
            lines.append(f"{record.name} {record.getMessage()} {json.dumps(fields, default=str)}")
        return "\n".join(lines)


class StravaIntegrationCase(unittest.TestCase):
    def setUp(self) -> None:
        self.upstream = SlowableUpstream(
            {"subscription_status": "ACTIVE", "capabilities": ["training.plan.manage", STRAVA]}
        )
        self.reply: httpx.Response = httpx.Response(200, json={})
        self.failure: type[Exception] | None = None
        self.requests: list[httpx.Request] = []

        def third_party(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            if self.failure is not None:
                raise self.failure("terceros")
            return self.reply

        settings = BffMobileSettings(
            app_env="test",  # type: ignore[arg-type]
            users_management_base_url="http://users.invalid",  # type: ignore[arg-type]
            affiliation_management_base_url="http://affiliation.invalid",  # type: ignore[arg-type]
            training_management_base_url="http://training.invalid",  # type: ignore[arg-type]
            events_management_base_url="http://events.invalid",  # type: ignore[arg-type]
            complementary_services_base_url="http://services.invalid",  # type: ignore[arg-type]
            third_party_management_base_url="http://third-party.invalid",  # type: ignore[arg-type]
        )
        users = UsersManagementClient(
            httpx.Client(
                base_url="http://users", transport=httpx.MockTransport(self.upstream.users)
            )
        )
        affiliation = AffiliationManagementClient(
            httpx.Client(
                base_url="http://affiliation",
                transport=httpx.MockTransport(self.upstream.affiliation),
            )
        )
        verifier = CachedJwksVerifier(
            users.jwks, issuer=settings.jwt_issuer, audience=settings.jwt_audience
        )
        app = create_service_app(
            service_name="bff-mobile",
            title="SportApp - BFF móvil",
            api_version="test",
            settings=settings,
            routers=[
                build_strava_integration_router(
                    verify_token=verifier.verify,
                    athlete_snapshot=affiliation.user_capabilities,
                    third_party=ThirdPartyManagementClient(
                        httpx.Client(
                            base_url="http://third-party",
                            transport=httpx.MockTransport(third_party),
                        )
                    ),
                )
            ],
        )
        self.client = TestClient(app)
        # `create_service_app` reemplaza los manejadores del log raíz: la captura va después.
        self.logs = RecordedLogs()
        logging.getLogger().addHandler(self.logs)
        self.addCleanup(logging.getLogger().removeHandler, self.logs)

    def auth(self, actor: str = "athlete", **headers: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token(actor)}", **headers}


class StravaIntegrationRoutesTest(StravaIntegrationCase):
    def send(self, operation: str, headers: dict[str, str]) -> httpx.Response:
        if operation == "authorize":
            return self.client.post(AUTHORIZATION_PATH, headers=headers)
        if operation == "status":
            return self.client.get(INTEGRATION_PATH, headers=headers)
        if operation == "unlink":
            return self.client.delete(INTEGRATION_PATH, headers=headers)
        return self.client.post(SYNCS_PATH, headers=headers)

    def every_operation(self, headers: dict[str, str]) -> dict[str, httpx.Response]:
        return {name: self.send(name, headers) for name in OPERATIONS}

    def test_authorization_is_relayed_without_cache(self) -> None:
        """CA1: la URL de autorización llega tal cual, sin caché; sin cuerpo ni clave."""
        correlation = str(uuid.uuid4())
        created = authorization_reply()
        self.reply = httpx.Response(201, json=created)

        response = self.client.post(
            AUTHORIZATION_PATH, headers=self.auth(**{"X-Correlation-ID": correlation})
        )

        self.assertEqual((response.status_code, response.json()), (201, created))
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        sent = self.requests[0]
        self.assertEqual(
            (sent.method, sent.url.path, sent.content), ("POST", AUTHORIZATION_PATH, b"")
        )
        self.assertTrue(sent.headers["Authorization"].startswith("Bearer "))
        self.assertEqual(sent.headers["X-Correlation-ID"], correlation)
        self.assertNotIn("Idempotency-Key", sent.headers)

    def test_status_is_relayed_without_cache(self) -> None:
        """CA2: estado de la vinculación y de la última corrida, tal como lo responde Terceros."""
        for document in (NOT_LINKED, LINKED):
            self.reply = httpx.Response(200, json=document)
            with self.subTest(status=document["status"]):
                response = self.client.get(INTEGRATION_PATH, headers=self.auth())
                self.assertEqual((response.status_code, response.json()), (200, document))
                self.assertEqual(response.headers["Cache-Control"], "no-store")
        sent = self.requests[-1]
        self.assertEqual((sent.method, sent.url.path, sent.content), ("GET", INTEGRATION_PATH, b""))

    def test_unlink_relays_no_content(self) -> None:
        """CA9: `DELETE` sin cuerpo ni clave; `204` sin cuerpo."""
        correlation = str(uuid.uuid4())
        self.reply = httpx.Response(204)

        response = self.client.delete(
            INTEGRATION_PATH, headers=self.auth(**{"X-Correlation-ID": correlation})
        )

        self.assertEqual((response.status_code, response.content), (204, b""))
        sent = self.requests[0]
        self.assertEqual(
            (sent.method, sent.url.path, sent.content), ("DELETE", INTEGRATION_PATH, b"")
        )
        self.assertNotIn("Idempotency-Key", sent.headers)
        self.assertEqual(sent.headers["X-Correlation-ID"], correlation)

    def test_sync_forwards_key_and_relays_replay(self) -> None:
        """CA4: clave y correlación tal cual; la misma clave → `202` con `Idempotent-Replayed`."""
        key, correlation = str(uuid.uuid4()), str(uuid.uuid4())
        self.reply = httpx.Response(202, json=QUEUED)
        accepted = self.client.post(
            SYNCS_PATH,
            headers=self.auth(**{"Idempotency-Key": key, "X-Correlation-ID": correlation}),
        )
        running = {**QUEUED, "status": "RUNNING"}
        self.reply = httpx.Response(202, json=running, headers={"Idempotent-Replayed": "true"})
        replayed = self.client.post(SYNCS_PATH, headers=self.auth(**{"Idempotency-Key": key}))

        self.assertEqual((accepted.status_code, accepted.json()), (202, QUEUED))
        self.assertNotIn("Idempotent-Replayed", accepted.headers)
        self.assertEqual((replayed.status_code, replayed.json()), (202, running))
        self.assertEqual(replayed.headers["Idempotent-Replayed"], "true")
        sent = self.requests[0]
        self.assertEqual((sent.method, sent.url.path, sent.content), ("POST", SYNCS_PATH, b""))
        self.assertEqual(sent.headers["Idempotency-Key"], key)
        self.assertEqual(sent.headers["X-Correlation-ID"], correlation)
        self.assertEqual(self.requests[1].headers["Idempotency-Key"], key)

    def test_sync_key_is_decided_by_the_service(self) -> None:
        """Sin clave no se inventa una; una clave inválida llega tal cual y la rechaza Terceros."""
        rejection = error("validation_error", [{"field": "Idempotency-Key", "issue": "required"}])
        self.reply = httpx.Response(422, json=rejection)
        missing = self.client.post(SYNCS_PATH, headers=self.auth())
        invalid = self.client.post(SYNCS_PATH, headers=self.auth(**{"Idempotency-Key": "no-uuid"}))

        self.assertEqual((missing.status_code, missing.json()), (422, rejection))
        self.assertEqual(invalid.status_code, 422)
        self.assertNotIn("Idempotency-Key", self.requests[0].headers)
        self.assertEqual(self.requests[1].headers["Idempotency-Key"], "no-uuid")

    def test_service_errors_pass_through_unchanged(self) -> None:
        """Estado, código y `details` de Terceros sin alterar, en cada operación."""
        cases = [
            ("sync", 409, error("sync_in_progress")),
            ("sync", 409, error("integration_not_linked")),
            (
                "sync",
                422,
                error("validation_error", [{"field": "Idempotency-Key", "issue": "invalid_uuid"}]),
            ),
            ("authorize", 503, error("upstream_unavailable")),
            ("status", 403, error("capability_required")),
            ("unlink", 401, error("invalid_access_token")),
            ("sync", 403, error("permission_required")),
            ("authorize", 504, error("upstream_timeout")),
        ]
        for operation, status, body in cases:
            self.reply = httpx.Response(status, json=body)
            with self.subTest(operation=operation, status=status, code=body["code"]):
                key = {"Idempotency-Key": str(uuid.uuid4())}
                response = self.send(operation, self.auth(**key))
                self.assertEqual((response.status_code, response.json()), (status, body))

    def test_missing_or_invalid_token(self) -> None:
        for name, response in {
            **{f"{k}-anonymous": v for k, v in self.every_operation({}).items()},
            **{
                f"{k}-invalid": v
                for k, v in self.every_operation({"Authorization": "Bearer roto"}).items()
            },
        }.items():
            with self.subTest(operation=name):
                self.assertEqual(
                    (response.status_code, response.json()["code"]), (401, "invalid_access_token")
                )
                self.assertEqual(response.headers["WWW-Authenticate"], "Bearer")
        self.assertEqual(self.requests, [])
        self.assertEqual(self.upstream.affiliation_paths, [])

    def test_other_actors_are_rejected_without_consulting_affiliation(self) -> None:
        for actor in ("organizer", "service_provider"):
            for name, response in self.every_operation(self.auth(actor)).items():
                with self.subTest(actor=actor, operation=name):
                    self.assertEqual(
                        (response.status_code, response.json()["code"]),
                        (403, "permission_required"),
                    )
        self.assertEqual(self.upstream.affiliation_paths, [])
        self.assertEqual(self.requests, [])

    def test_every_operation_requires_the_strava_capability(self) -> None:
        """CA10/ADR-024: sin `integrations.strava.connect` vigente → `403 capability_required`."""
        snapshots = [
            {"subscription_status": "ACTIVE", "capabilities": ["training.plan.manage"]},
            {"subscription_status": "NONE", "capabilities": [STRAVA]},
            {"subscription_status": "ACTIVE", "capabilities": None},
        ]
        for snapshot in snapshots:
            self.upstream.snapshot = snapshot
            for name, response in self.every_operation(self.auth()).items():
                with self.subTest(snapshot=snapshot, operation=name):
                    self.assertEqual(
                        (response.status_code, response.json()["code"]),
                        (403, "capability_required"),
                    )
        self.assertEqual(self.requests, [])
        self.assertTrue(self.upstream.affiliation_paths)

    def test_affiliation_down_or_slow(self) -> None:
        self.upstream.status = 0
        down = self.every_operation(self.auth())
        self.upstream.status, self.upstream.slow = 200, True
        slow = self.every_operation(self.auth())

        for name in down:
            with self.subTest(operation=name):
                self.assertEqual(
                    (down[name].status_code, down[name].json()["code"]),
                    (503, "upstream_unavailable"),
                )
                self.assertEqual(
                    (slow[name].status_code, slow[name].json()["code"]), (504, "upstream_timeout")
                )
        self.assertEqual(self.requests, [])

    def test_jwks_unavailable(self) -> None:
        self.upstream.jwks_down = True

        response = self.client.get(INTEGRATION_PATH, headers=self.auth())

        self.assertEqual(
            (response.status_code, response.json()["code"]), (503, "upstream_unavailable")
        )
        self.assertEqual(self.requests, [])

    def test_third_party_unreachable(self) -> None:
        self.failure = httpx.ReadTimeout
        timeout = self.every_operation(self.auth())
        self.failure = httpx.ConnectError
        down = self.every_operation(self.auth())

        for name in timeout:
            with self.subTest(operation=name):
                self.assertEqual(
                    (timeout[name].status_code, timeout[name].json()["code"]),
                    (504, "upstream_timeout"),
                )
                self.assertEqual(
                    (down[name].status_code, down[name].json()["code"]),
                    (503, "upstream_unavailable"),
                )

    def test_unexpected_statuses_become_unavailable(self) -> None:
        """Estados que el contrato no prevé (o un cuerpo no JSON) → `503` y log de aviso."""
        cases = [
            ("authorize", httpx.Response(200, json=authorization_reply())),
            ("status", httpx.Response(404, json=error("not_found"))),
            ("status", httpx.Response(500, json=error("internal_error"))),
            ("unlink", httpx.Response(200, json={})),
            ("unlink", httpx.Response(409, json=error("conflict"))),
            ("sync", httpx.Response(201, json=QUEUED)),
            ("sync", httpx.Response(202, content=b"no es json")),
        ]
        for operation, reply in cases:
            self.reply = reply
            self.logs.records.clear()
            with self.subTest(operation=operation, status=reply.status_code):
                response = self.send(operation, self.auth())
                self.assertEqual(
                    (response.status_code, response.json()["code"]), (503, "upstream_unavailable")
                )
                self.assertIn("bff.upstream_unexpected_status", self.logs.text())
                self.assertIn("third-party-management", self.logs.text())
        self.reply = httpx.Response(500)
        status = self.client.get(INTEGRATION_PATH, headers=self.auth())
        self.assertEqual((status.status_code, status.headers["Cache-Control"]), (503, "no-store"))

    def test_logs_never_contain_the_authorization_url_or_state(self) -> None:
        """ADR-042: la URL de autorización lleva el `state`; nunca se registra."""
        created = authorization_reply()
        self.reply = httpx.Response(201, json=created)
        self.client.post(AUTHORIZATION_PATH, headers=self.auth())

        logged = self.logs.text()
        self.assertIn("request.completed", logged, "la captura de logs funciona")
        self.assertNotIn(created["authorization_url"].rsplit("state=", 1)[1], logged)
        self.assertNotIn("strava.com", logged)


class StravaCallbackTest(StravaIntegrationCase):
    def callback(self, **parameters: str) -> httpx.Response:
        return self.client.get(CALLBACK_PATH, params=parameters, follow_redirects=False)

    def assert_redirects_to(self, response: httpx.Response, result: str) -> None:
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], f"{DEEP_LINK}{result}")
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_each_result_redirects_to_the_deep_link(self) -> None:
        """CA1/CA3: `LINKED`, `DENIED`, `INVALID_STATE` y `ERROR` de Terceros, sin alterar."""
        for result in RESULTS:
            self.reply = httpx.Response(200, json={"result": result})
            with self.subTest(result=result):
                response = self.callback(
                    code=secrets.token_urlsafe(16),
                    state=secrets.token_urlsafe(32),
                    scope="read,activity:read",
                )
                self.assert_redirects_to(response, result)

    def test_parameters_are_forwarded_as_json_without_token(self) -> None:
        """Público: sin token de SportApp, sin Afiliación; los parámetros van en el cuerpo."""
        code, state = secrets.token_urlsafe(16), secrets.token_urlsafe(32)
        self.reply = httpx.Response(200, json={"result": "LINKED"})

        response = self.client.get(
            CALLBACK_PATH,
            params={"code": code, "state": state, "scope": "read,activity:read"},
            headers={"Authorization": "Bearer roto"},
            follow_redirects=False,
        )

        self.assert_redirects_to(response, "LINKED")
        sent = self.requests[0]
        self.assertEqual((sent.method, sent.url.path), ("POST", CALLBACK_PATH))
        self.assertEqual(sent.url.query, b"", "nada sensible en la URL interna")
        self.assertEqual(
            json.loads(sent.content),
            {"code": code, "state": state, "scope": "read,activity:read", "error": None},
        )
        self.assertNotIn("Authorization", sent.headers)
        self.assertEqual(sent.headers["X-Correlation-ID"], response.headers["X-Correlation-ID"])
        self.assertEqual(self.upstream.affiliation_paths, [])

    def test_denial_is_forwarded(self) -> None:
        """CA3: `error=access_denied` sin `code` llega a Terceros, que decide `DENIED`."""
        state = secrets.token_urlsafe(32)
        self.reply = httpx.Response(200, json={"result": "DENIED"})

        response = self.callback(state=state, error="access_denied")

        self.assert_redirects_to(response, "DENIED")
        self.assertEqual(
            json.loads(self.requests[0].content),
            {"code": None, "state": state, "scope": None, "error": "access_denied"},
        )

    def test_missing_parameters_are_forwarded_as_null(self) -> None:
        self.reply = httpx.Response(200, json={"result": "INVALID_STATE"})

        response = self.callback()

        self.assert_redirects_to(response, "INVALID_STATE")
        self.assertEqual(
            json.loads(self.requests[0].content),
            {"code": None, "state": None, "scope": None, "error": None},
        )

    def test_long_parameters_are_decided_by_the_service(self) -> None:
        """El BFF no recorta ni rechaza: Terceros responde `422` y el resultado es `ERROR`."""
        state = "s" * 600
        self.reply = httpx.Response(
            422, json=error("validation_error", [{"field": "state", "issue": "invalid_length"}])
        )

        response = self.callback(state=state)

        self.assert_redirects_to(response, "ERROR")
        self.assertEqual(json.loads(self.requests[0].content)["state"], state)

    def test_any_third_party_failure_is_error(self) -> None:
        """Terceros caído, lento, con otro estado, cuerpo ilegible o `result` desconocido."""
        replies = {
            "500": httpx.Response(500, json=error("internal_error")),
            "422": httpx.Response(422, json=error("validation_error")),
            "201": httpx.Response(201, json={"result": "LINKED"}),
            "no-json": httpx.Response(200, content=b"no es json"),
            "lista": httpx.Response(200, json=["LINKED"]),
            "sin-result": httpx.Response(200, json={}),
            "desconocido": httpx.Response(200, json={"result": "linked"}),
            "no-texto": httpx.Response(200, json={"result": 1}),
        }
        for name, reply in replies.items():
            self.reply = reply
            with self.subTest(reply=name):
                self.assert_redirects_to(self.callback(state=secrets.token_urlsafe(32)), "ERROR")
        self.reply = httpx.Response(200, json={"result": "LINKED"})
        for failure in (httpx.ReadTimeout, httpx.ConnectError):
            self.failure = failure
            with self.subTest(failure=failure.__name__):
                self.assert_redirects_to(self.callback(state=secrets.token_urlsafe(32)), "ERROR")

    def test_logs_contain_only_the_result(self) -> None:
        """ADR-042: `strava.callback` solo con el `result`; nunca `code`, `state` ni `scope`."""
        code, state = secrets.token_urlsafe(16), secrets.token_urlsafe(32)
        scope = "read,activity:read"
        self.reply = httpx.Response(200, json={"result": "LINKED"})
        self.callback(code=code, state=state, scope=scope)
        self.reply = httpx.Response(500, json={"code": code, "state": state})
        self.callback(code=code, state=state, scope=scope)
        self.failure = httpx.ConnectError
        self.callback(code=code, state=state, scope=scope)

        logged = self.logs.text()
        self.assertIn("request.completed", logged, "la captura de logs funciona")
        self.assertEqual(
            [getattr(r, "fields", None) for r in self.records("strava.callback")],
            [{"result": "LINKED"}, {"result": "ERROR"}, {"result": "ERROR"}],
        )
        for secret in (code, state, scope, "activity:read", "code=", "state="):
            with self.subTest(secret=secret):
                self.assertNotIn(secret, logged)

    def records(self, message: str) -> list[logging.LogRecord]:
        return [r for r in self.logs.records if r.getMessage() == message]


if __name__ == "__main__":
    unittest.main()
