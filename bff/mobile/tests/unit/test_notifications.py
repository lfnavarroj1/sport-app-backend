"""BFF móvil, HU039: bandeja, preferencias y token push se reenvían sin alterar (ADR-045 §11).

El borde exige `athlete`; lo demás llega de `events_management` tal cual: parámetros, cuerpo
(bytes), correlación, respuestas y errores. Un estado no previsto es `upstream_unavailable`.
El router se monta solo, con los mismos clientes que `create_app`, para probar el borde.
"""

import json
import logging
import unittest
import uuid
from typing import Any

import httpx
from fastapi.testclient import TestClient
from test_capabilities import Upstream, token

from bff_mobile.api.notifications import build_notifications_router
from bff_mobile.clients.events_management import EventsManagementClient
from bff_mobile.clients.users_management import UsersManagementClient
from bff_mobile.settings import BffMobileSettings
from sportapp_platform.app import create_service_app
from sportapp_platform.tokens import CachedJwksVerifier

NOTIFICATION_ID = str(uuid.uuid4())
EVENT_ID = str(uuid.uuid4())
PUSH_TOKEN = "token-fcm-sintetico-" + "x" * 40
NOTIFICATION = {
    "notification_id": NOTIFICATION_ID,
    "type": "EVENT_PUBLISHED_MATCH",
    "priority": "NORMAL",
    "event_id": EVENT_ID,
    "title": "Nuevo evento: Carrera 10K Bogotá",
    "body": "running en Bogotá. Toca para ver el detalle.",
    "created_at": "2026-10-24T12:00:00Z",
    "read_at": None,
}
PREFERENCES = {"event_matches": True, "event_reminders": True, "push_enabled": True, "version": 0}
PREFERENCES_BODY = (
    b'{"event_matches": false, "event_reminders": true, "push_enabled": false, '
    b'"expected_version": 0}'
)
PUSH_BODY = json.dumps({"token": PUSH_TOKEN, "platform": "android"}).encode()
LIST_PATH = "/v1/me/notifications"
READ_PATH = f"/v1/me/notifications/{NOTIFICATION_ID}/read"
PREFERENCES_PATH = "/v1/me/notification-preferences"
PUSH_PATH = "/v1/me/push-tokens"
# Estados que cada operación entrega tal cual (ADR-045 §11); el resto es `503`.
ALLOWED = {
    "list": (200, 401, 403, 422, 503),
    "read": (200, 401, 403, 404, 422, 503),
    "get_preferences": (200, 401, 403, 503),
    "put_preferences": (200, 401, 403, 409, 422, 503),
    "push": (204, 401, 403, 409, 422, 503),
}


class RecordedLogs(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def text(self) -> str:
        return "\n".join(
            f"{record.getMessage()} {json.dumps(getattr(record, 'fields', {}), default=str)}"
            for record in self.records
        )


class NotificationRoutesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.upstream = Upstream()
        self.reply: httpx.Response = httpx.Response(200, json={})
        self.failure: type[Exception] | None = None
        self.requests: list[httpx.Request] = []

        def events(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            if self.failure is not None:
                raise self.failure("eventos")
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
        verifier = CachedJwksVerifier(
            users.jwks, issuer=settings.jwt_issuer, audience=settings.jwt_audience
        )
        app = create_service_app(
            service_name="bff-mobile",
            title="SportApp - BFF móvil",
            api_version="test",
            settings=settings,
            routers=[
                build_notifications_router(
                    verify_token=verifier.verify,
                    events=EventsManagementClient(
                        httpx.Client(
                            base_url="http://events", transport=httpx.MockTransport(events)
                        )
                    ),
                )
            ],
        )
        self.client = TestClient(app)
        # Después de crear la app: `create_service_app` reemplaza los manejadores raíz.
        self.logs = RecordedLogs()
        logging.getLogger().addHandler(self.logs)
        self.addCleanup(logging.getLogger().removeHandler, self.logs)

    def auth(self, actor: str = "athlete", **headers: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token(actor)}", **headers}

    def send(self, operation: str, headers: dict[str, str]) -> httpx.Response:
        if operation == "list":
            return self.client.get(LIST_PATH, headers=headers)
        if operation == "read":
            return self.client.post(READ_PATH, headers=headers)
        if operation == "get_preferences":
            return self.client.get(PREFERENCES_PATH, headers=headers)
        if operation == "put_preferences":
            return self.client.put(PREFERENCES_PATH, content=PREFERENCES_BODY, headers=headers)
        return self.client.put(PUSH_PATH, content=PUSH_BODY, headers=headers)

    def every_operation(self, headers: dict[str, str]) -> dict[str, httpx.Response]:
        return {operation: self.send(operation, headers) for operation in ALLOWED}

    def test_list_forwards_token_correlation_and_parameters(self) -> None:
        """CA4: la bandeja propia se entrega tal cual, con `limit` y `cursor` reenviados."""
        correlation = str(uuid.uuid4())
        page = {"items": [NOTIFICATION], "next_cursor": "siguiente"}
        self.reply = httpx.Response(200, json=page, headers={"Cache-Control": "no-store"})
        headers = self.auth(**{"X-Correlation-ID": correlation})

        listed = self.client.get(f"{LIST_PATH}?limit=5&cursor=abc", headers=headers)
        unfiltered = self.client.get(LIST_PATH, headers=self.auth())

        self.assertEqual((listed.status_code, listed.json()), (200, page))
        self.assertEqual(listed.headers["Cache-Control"], "no-store")
        sent = self.requests[0]
        self.assertEqual((sent.method, sent.url.path), ("GET", LIST_PATH))
        self.assertEqual(dict(sent.url.params), {"limit": "5", "cursor": "abc"})
        self.assertEqual(sent.headers["Authorization"], headers["Authorization"])
        self.assertEqual(sent.headers["X-Correlation-ID"], correlation)
        self.assertEqual(unfiltered.status_code, 200)
        self.assertEqual(dict(self.requests[1].url.params), {})

    def test_mark_read_forwards_the_identifier_without_body(self) -> None:
        """CA4: marcar como leída devuelve la notificación con `read_at`."""
        read = {**NOTIFICATION, "read_at": "2026-10-24T12:05:00Z"}
        self.reply = httpx.Response(200, json=read)

        response = self.client.post(READ_PATH, headers=self.auth())

        self.assertEqual((response.status_code, response.json()), (200, read))
        sent = self.requests[0]
        self.assertEqual((sent.method, sent.url.path), ("POST", READ_PATH))
        self.assertEqual(sent.content, b"")

    def test_invalid_notification_id_is_decided_by_the_service(self) -> None:
        """El identificador viaja como un único segmento; el `422` es el del servicio."""
        error = {
            "code": "validation_error",
            "message": "m",
            "correlation_id": "c",
            "details": [{"field": "path.notification_id", "issue": "invalid_uuid"}],
        }
        self.reply = httpx.Response(422, json=error)

        response = self.client.post("/v1/me/notifications/no-es-uuid%3Fx/read", headers=self.auth())

        self.assertEqual((response.status_code, response.json()), (422, error))
        self.assertEqual(self.requests[0].url.raw_path, b"/v1/me/notifications/no-es-uuid%3Fx/read")

    def test_preferences_are_read_and_replaced_with_the_raw_body(self) -> None:
        """CA2: preferencias vigentes y reemplazo con `expected_version`; el cuerpo no se
        reinterpreta."""
        self.reply = httpx.Response(200, json=PREFERENCES)
        current = self.client.get(PREFERENCES_PATH, headers=self.auth())
        replaced = {**PREFERENCES, "event_matches": False, "push_enabled": False, "version": 1}
        self.reply = httpx.Response(200, json=replaced)
        correlation = str(uuid.uuid4())
        headers = self.auth(**{"X-Correlation-ID": correlation})
        updated = self.client.put(PREFERENCES_PATH, content=PREFERENCES_BODY, headers=headers)

        self.assertEqual((current.status_code, current.json()), (200, PREFERENCES))
        self.assertEqual((updated.status_code, updated.json()), (200, replaced))
        sent = self.requests[1]
        self.assertEqual((sent.method, sent.url.path), ("PUT", PREFERENCES_PATH))
        self.assertEqual(sent.content, PREFERENCES_BODY)
        self.assertEqual(sent.headers["Content-Type"], "application/json")
        # El reenvío corre en el pool de hilos: la correlación y el token llegan igual.
        self.assertEqual(sent.headers["X-Correlation-ID"], correlation)
        self.assertEqual(sent.headers["Authorization"], headers["Authorization"])

    def test_malformed_body_reaches_the_service(self) -> None:
        """Ni un JSON mal formado ni un cuerpo vacío se validan en el borde."""
        error = {"code": "validation_error", "message": "m", "correlation_id": "c", "details": []}
        self.reply = httpx.Response(422, json=error)

        broken = self.client.put(PREFERENCES_PATH, content=b"{no-json", headers=self.auth())
        empty = self.client.put(PUSH_PATH, headers=self.auth())

        self.assertEqual((broken.status_code, broken.json()), (422, error))
        self.assertEqual((empty.status_code, empty.json()), (422, error))
        self.assertEqual(self.requests[0].content, b"{no-json")
        self.assertEqual(self.requests[1].content, b"")
        self.assertNotIn("Content-Type", self.requests[1].headers)

    def test_push_token_is_registered_with_an_empty_204(self) -> None:
        """CA5: el token del dispositivo viaja tal cual y la respuesta queda vacía."""
        self.reply = httpx.Response(204)

        response = self.client.put(PUSH_PATH, content=PUSH_BODY, headers=self.auth())

        self.assertEqual((response.status_code, response.content), (204, b""))
        sent = self.requests[0]
        self.assertEqual((sent.method, sent.url.path), ("PUT", PUSH_PATH))
        self.assertEqual(sent.content, PUSH_BODY)
        self.assertNotIn(PUSH_TOKEN, self.logs.text())

    def test_service_errors_pass_through_unchanged(self) -> None:
        """Los códigos y `details` del servicio llegan sin reinterpretar."""
        cases: list[tuple[str, int, dict[str, Any]]] = [
            ("list", 422, {"code": "validation_error", "details": [{"field": "query.limit"}]}),
            ("read", 404, {"code": "notification_not_found"}),
            (
                "put_preferences",
                409,
                {
                    "code": "version_conflict",
                    "details": [{"field": "expected_version", "issue": "3"}],
                },
            ),
            ("push", 409, {"code": "push_not_enabled"}),
            ("get_preferences", 503, {"code": "upstream_unavailable"}),
            ("get_preferences", 401, {"code": "invalid_access_token"}),
        ]
        for operation, status, body in cases:
            payload = {"message": "m", "correlation_id": "c", **body}
            self.reply = httpx.Response(status, json=payload)
            with self.subTest(operation=operation, code=body["code"]):
                response = self.send(operation, self.auth())
                self.assertEqual((response.status_code, response.json()), (status, payload))

    def test_every_allowed_status_is_delivered(self) -> None:
        for operation, statuses in ALLOWED.items():
            for status in statuses:
                self.reply = (
                    httpx.Response(204)
                    if status == 204
                    else httpx.Response(status, json={"code": f"estado_{status}"})
                )
                with self.subTest(operation=operation, status=status):
                    self.assertEqual(self.send(operation, self.auth()).status_code, status)

    def test_unexpected_status_is_upstream_unavailable(self) -> None:
        unexpected = {
            "list": 404,
            "read": 409,
            "get_preferences": 422,
            "put_preferences": 404,
            "push": 200,
        }
        for operation, status in unexpected.items():
            self.reply = httpx.Response(status, json={"code": "otro"})
            with self.subTest(operation=operation):
                response = self.send(operation, self.auth())
                self.assertEqual(
                    (response.status_code, response.json()["code"]), (503, "upstream_unavailable")
                )
        self.reply = httpx.Response(500, text="fallo")
        self.assertEqual(self.send("list", self.auth()).status_code, 503)
        self.assertIn("bff.upstream_unexpected_status", self.logs.text())
        self.assertIn('"status": 500', self.logs.text())

    def test_edge_rejections_never_reach_the_service(self) -> None:
        organizer = self.every_operation(self.auth("organizer"))
        anonymous = self.every_operation({})
        invalid = self.every_operation({"Authorization": "Bearer roto"})

        for operation in ALLOWED:
            with self.subTest(operation=operation):
                self.assertEqual(
                    (organizer[operation].status_code, organizer[operation].json()["code"]),
                    (403, "permission_required"),
                )
                self.assertEqual(
                    (anonymous[operation].status_code, anonymous[operation].json()["code"]),
                    (401, "invalid_access_token"),
                )
                self.assertEqual(anonymous[operation].headers["WWW-Authenticate"], "Bearer")
                self.assertEqual(invalid[operation].status_code, 401)
        self.assertEqual(self.requests, [])

    def test_jwks_unavailable(self) -> None:
        self.upstream.jwks_down = True

        responses = self.every_operation(self.auth())

        for operation, response in responses.items():
            with self.subTest(operation=operation):
                self.assertEqual(
                    (response.status_code, response.json()["code"]), (503, "upstream_unavailable")
                )
        self.assertEqual(self.requests, [])

    def test_events_unreachable(self) -> None:
        self.failure = httpx.ReadTimeout
        slow = self.every_operation(self.auth())
        self.failure = httpx.ConnectError
        down = self.every_operation(self.auth())

        for operation in ALLOWED:
            with self.subTest(operation=operation):
                self.assertEqual(
                    (slow[operation].status_code, slow[operation].json()["code"]),
                    (504, "upstream_timeout"),
                )
                self.assertEqual(
                    (down[operation].status_code, down[operation].json()["code"]),
                    (503, "upstream_unavailable"),
                )


if __name__ == "__main__":
    unittest.main()
