"""BFF móvil, HU037: dispositivos de medición y mediciones en vivo (ADR-044).

El borde exige `athlete` y la capacidad vigente de cada operación
(`devices.measurement.connect` o `training.realtime.monitor`); lo demás llega del servicio sin
alterar: cuerpo, `Idempotency-Key`, correlación, `Location`, `Idempotent-Replayed` y errores.
El router se monta solo, con los mismos clientes que `create_app`, para probar el borde.
"""

import hashlib
import json
import logging
import unittest
import uuid
from typing import Any

import httpx
from fastapi.testclient import TestClient
from test_capabilities import Upstream, token

from bff_mobile.api.devices import build_devices_router
from bff_mobile.clients.affiliation_management import AffiliationManagementClient
from bff_mobile.clients.training_management import TrainingManagementClient
from bff_mobile.clients.users_management import UsersManagementClient
from bff_mobile.settings import BffMobileSettings
from sportapp_platform.app import create_service_app
from sportapp_platform.tokens import CachedJwksVerifier

DEVICES = "devices.measurement.connect"
REALTIME = "training.realtime.monitor"
DEVICE_ID = hashlib.sha256(b"sintetico-AA:BB:CC:DD:EE:FF").hexdigest()
SESSION_ID = str(uuid.uuid4())
HEART_RATE = 173
REGISTER = {
    "device_id": DEVICE_ID,
    "display_name": "Banda de pecho",
    "device_type": "HEART_RATE_MONITOR",
    "protocol": "BLE_HRS",
}
DEVICE = {
    **REGISTER,
    "athlete_id": str(uuid.uuid4()),
    "status": "ACTIVE",
    "version": 1,
    "created_at": "2026-10-24T12:00:00Z",
    "updated_at": "2026-10-24T12:00:00Z",
    "revoked_at": None,
}
REVOKED = {**DEVICE, "status": "REVOKED", "version": 2, "revoked_at": "2026-10-24T13:00:00Z"}
BATCH = {
    "device_id": DEVICE_ID,
    "samples": [
        {"measured_at": "2026-10-24T12:30:05Z", "type": "HEART_RATE", "value": HEART_RATE},
        {"measured_at": "2026-10-24T12:30:06Z", "type": "HEART_RATE", "value": 999, "unit": "bpm"},
    ],
}
BATCH_RESULT = {
    "session_id": SESSION_ID,
    "device_id": DEVICE_ID,
    "received": 2,
    "accepted": 0,
    "duplicates": 0,
    "rejected": [{"index": 0, "issue": "required"}, {"index": 1, "issue": "out_of_range"}],
}
MEASUREMENTS_PATH = f"/v1/training-sessions/{SESSION_ID}/measurements"
OPERATIONS = ("register", "list", "revoke", "ingest")


def error(code: str, details: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {"code": code, "message": "m", "correlation_id": "c", "details": details or []}


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


class DevicesRoutesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.upstream = SlowableUpstream(
            {"subscription_status": "ACTIVE", "capabilities": [DEVICES, REALTIME]}
        )
        self.reply: httpx.Response = httpx.Response(200, json={})
        self.failure: type[Exception] | None = None
        self.requests: list[httpx.Request] = []

        def training(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            if self.failure is not None:
                raise self.failure("entrenamientos")
            return self.reply

        settings = BffMobileSettings(
            app_env="test",  # type: ignore[arg-type]
            users_management_base_url="http://users.invalid",  # type: ignore[arg-type]
            affiliation_management_base_url="http://affiliation.invalid",  # type: ignore[arg-type]
            training_management_base_url="http://training.invalid",  # type: ignore[arg-type]
            events_management_base_url="http://events.invalid",  # type: ignore[arg-type]
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
                build_devices_router(
                    verify_token=verifier.verify,
                    athlete_snapshot=affiliation.user_capabilities,
                    training=TrainingManagementClient(
                        httpx.Client(
                            base_url="http://training", transport=httpx.MockTransport(training)
                        )
                    ),
                )
            ],
        )
        self.client = TestClient(app)
        self.logs = RecordedLogs()
        logging.getLogger().addHandler(self.logs)
        self.addCleanup(logging.getLogger().removeHandler, self.logs)

    def auth(self, actor: str = "athlete", **headers: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token(actor)}", **headers}

    def register(self, **headers: str) -> httpx.Response:
        return self.client.post("/v1/me/devices", json=REGISTER, headers=self.auth(**headers))

    def send(self, operation: str, headers: dict[str, str]) -> httpx.Response:
        if operation == "register":
            return self.client.post("/v1/me/devices", json=REGISTER, headers=headers)
        if operation == "list":
            return self.client.get("/v1/me/devices", headers=headers)
        if operation == "revoke":
            return self.client.delete(f"/v1/me/devices/{DEVICE_ID}", headers=headers)
        return self.client.post(MEASUREMENTS_PATH, json=BATCH, headers=headers)

    def every_operation(self, headers: dict[str, str]) -> dict[str, httpx.Response]:
        return {name: self.send(name, headers) for name in OPERATIONS}

    def test_register_forwards_body_key_and_correlation(self) -> None:
        """CA1: cuerpo, clave y correlación llegan tal cual; `Location` vuelve sin alterar."""
        key, correlation = str(uuid.uuid4()), str(uuid.uuid4())
        location = f"/v1/me/devices/{DEVICE_ID}"
        self.reply = httpx.Response(201, json=DEVICE, headers={"Location": location})

        response = self.register(**{"Idempotency-Key": key, "X-Correlation-ID": correlation})

        self.assertEqual((response.status_code, response.json()), (201, DEVICE))
        self.assertEqual(response.headers["Location"], location)
        self.assertNotIn("Idempotent-Replayed", response.headers)
        sent = self.requests[0]
        self.assertEqual((sent.method, sent.url.path), ("POST", "/v1/me/devices"))
        self.assertEqual(json.loads(sent.content), REGISTER)
        self.assertEqual(sent.headers["Idempotency-Key"], key)
        self.assertEqual(sent.headers["X-Correlation-ID"], correlation)
        self.assertTrue(sent.headers["Authorization"].startswith("Bearer "))

    def test_register_replays_and_existing_device_are_relayed(self) -> None:
        """Misma clave → `201` con `Idempotent-Replayed`; ya activo con el mismo nombre → `200`."""
        self.reply = httpx.Response(201, json=DEVICE, headers={"Idempotent-Replayed": "true"})
        replayed = self.register(**{"Idempotency-Key": str(uuid.uuid4())})
        self.reply = httpx.Response(200, json=DEVICE)
        existing = self.register(**{"Idempotency-Key": str(uuid.uuid4())})

        self.assertEqual(replayed.status_code, 201)
        self.assertEqual(replayed.headers["Idempotent-Replayed"], "true")
        self.assertEqual((existing.status_code, existing.json()), (200, DEVICE))

    def test_register_body_is_not_reinterpreted(self) -> None:
        """Sin clave no se inventa una; un campo de más llega al servicio, que lo rechaza."""
        rejection = error(
            "validation_error", [{"field": "athlete_id", "issue": "field_not_allowed"}]
        )
        self.reply = httpx.Response(422, json=rejection)
        body = {**REGISTER, "athlete_id": str(uuid.uuid4())}

        response = self.client.post("/v1/me/devices", json=body, headers=self.auth())

        self.assertEqual((response.status_code, response.json()), (422, rejection))
        self.assertEqual(json.loads(self.requests[0].content), body)
        self.assertNotIn("Idempotency-Key", self.requests[0].headers)

    def test_list_is_relayed_without_cache(self) -> None:
        """CA2: dispositivos propios con `Cache-Control: no-store`."""
        listed = {"items": [DEVICE, REVOKED]}
        self.reply = httpx.Response(200, json=listed)

        response = self.client.get("/v1/me/devices", headers=self.auth())

        self.assertEqual((response.status_code, response.json()), (200, listed))
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        sent = self.requests[0]
        self.assertEqual((sent.method, sent.url.path, sent.content), ("GET", "/v1/me/devices", b""))

    def test_revoke_sends_delete_without_body_or_key(self) -> None:
        """CA3: revocar es un `DELETE` sin cuerpo ni clave; la respuesta llega sin alterar."""
        correlation = str(uuid.uuid4())
        self.reply = httpx.Response(200, json=REVOKED)

        response = self.client.delete(
            f"/v1/me/devices/{DEVICE_ID}",
            headers=self.auth(**{"X-Correlation-ID": correlation}),
        )

        self.assertEqual((response.status_code, response.json()), (200, REVOKED))
        sent = self.requests[0]
        self.assertEqual((sent.method, sent.url.path), ("DELETE", f"/v1/me/devices/{DEVICE_ID}"))
        self.assertEqual(sent.content, b"")
        self.assertNotIn("Idempotency-Key", sent.headers)
        self.assertEqual(sent.headers["X-Correlation-ID"], correlation)

    def test_revoke_encodes_the_path_segment_without_validating_it(self) -> None:
        """El formato lo decide el servicio; `?` no puede cortar la ruta reenviada."""
        self.reply = httpx.Response(
            422, json=error("validation_error", [{"field": "device_id", "issue": "invalid_format"}])
        )

        response = self.client.delete("/v1/me/devices/no%3Fhex", headers=self.auth())

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["details"][0]["issue"], "invalid_format")
        self.assertEqual(self.requests[0].url.raw_path, b"/v1/me/devices/no%3Fhex")

    def test_ingest_forwards_the_batch_as_is(self) -> None:
        """CA4/CA5: lote tal cual, sin clave; aceptadas, duplicadas y rechazadas del servicio."""
        correlation = str(uuid.uuid4())
        self.reply = httpx.Response(200, json=BATCH_RESULT)

        response = self.client.post(
            MEASUREMENTS_PATH,
            json=BATCH,
            headers=self.auth(
                **{"X-Correlation-ID": correlation, "Idempotency-Key": str(uuid.uuid4())}
            ),
        )

        self.assertEqual((response.status_code, response.json()), (200, BATCH_RESULT))
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        sent = self.requests[0]
        self.assertEqual((sent.method, sent.url.path), ("POST", MEASUREMENTS_PATH))
        self.assertEqual(json.loads(sent.content), BATCH)
        self.assertNotIn("Idempotency-Key", sent.headers, "idempotencia natural (ADR-044)")
        self.assertEqual(sent.headers["X-Correlation-ID"], correlation)

    def test_ingest_without_body_reaches_the_service(self) -> None:
        """Cuerpo ausente: lo rechaza el servicio con `validation_error`."""
        rejection = error("validation_error", [{"field": "body", "issue": "required"}])
        self.reply = httpx.Response(422, json=rejection)

        response = self.client.post(MEASUREMENTS_PATH, headers=self.auth())

        self.assertEqual((response.status_code, response.json()), (422, rejection))
        self.assertEqual(self.requests[0].content, b"")

    def test_service_errors_pass_through_unchanged(self) -> None:
        """Estado, código y `details` del servicio sin alterar, en cada operación."""
        cases = [
            ("register", 409, error("device_already_registered")),
            ("register", 409, error("idempotency_key_reused")),
            (
                "register",
                422,
                error("validation_error", [{"field": "display_name", "issue": "invalid_length"}]),
            ),
            ("revoke", 404, error("device_not_found")),
            ("ingest", 404, error("training_session_not_found")),
            ("ingest", 404, error("device_not_found")),
            ("ingest", 409, error("training_session_already_finished")),
            ("ingest", 409, error("device_revoked")),
            (
                "ingest",
                422,
                error("validation_error", [{"field": "samples", "issue": "too_many_items"}]),
            ),
            ("list", 403, error("capability_required")),
            ("ingest", 401, error("invalid_access_token")),
            ("revoke", 503, error("upstream_unavailable")),
            ("register", 504, error("upstream_timeout")),
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

    def test_each_operation_requires_its_capability(self) -> None:
        """Dispositivos: `devices.measurement.connect`; mediciones: `training.realtime.monitor`."""
        self.reply = httpx.Response(200, json=BATCH_RESULT)
        self.upstream.snapshot = {"subscription_status": "ACTIVE", "capabilities": [DEVICES]}
        only_devices = self.every_operation(self.auth())
        self.upstream.snapshot = {"subscription_status": "ACTIVE", "capabilities": [REALTIME]}
        only_realtime = self.every_operation(self.auth())

        for name in ("register", "list", "revoke"):
            with self.subTest(operation=name, granted=DEVICES):
                self.assertNotEqual(only_devices[name].status_code, 403)
            with self.subTest(operation=name, granted=REALTIME):
                self.assertEqual(
                    (only_realtime[name].status_code, only_realtime[name].json()["code"]),
                    (403, "capability_required"),
                )
        self.assertEqual(
            (only_devices["ingest"].status_code, only_devices["ingest"].json()["code"]),
            (403, "capability_required"),
        )
        self.assertEqual(only_realtime["ingest"].status_code, 200)
        self.assertEqual(
            [(r.method, r.url.path) for r in self.requests],
            [
                ("POST", "/v1/me/devices"),
                ("GET", "/v1/me/devices"),
                ("DELETE", f"/v1/me/devices/{DEVICE_ID}"),
                ("POST", MEASUREMENTS_PATH),
            ],
        )

    def test_inactive_subscription_has_no_capability(self) -> None:
        self.upstream.snapshot = {
            "subscription_status": "NONE",
            "capabilities": [DEVICES, REALTIME],
        }

        for name, response in self.every_operation(self.auth()).items():
            with self.subTest(operation=name):
                self.assertEqual(
                    (response.status_code, response.json()["code"]), (403, "capability_required")
                )
        self.assertEqual(self.requests, [])

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

        response = self.client.get("/v1/me/devices", headers=self.auth())

        self.assertEqual(
            (response.status_code, response.json()["code"]), (503, "upstream_unavailable")
        )
        self.assertEqual(self.requests, [])

    def test_training_unreachable(self) -> None:
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
            ("register", httpx.Response(404, json=error("not_found"))),
            ("list", httpx.Response(201, json={"items": []})),
            ("list", httpx.Response(500, json=error("internal_error"))),
            ("revoke", httpx.Response(409, json=error("conflict"))),
            ("ingest", httpx.Response(201, json=BATCH_RESULT)),
            ("ingest", httpx.Response(200, content=b"no es json")),
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
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.reply = httpx.Response(500)
        listed = self.client.get("/v1/me/devices", headers=self.auth())
        self.assertEqual((listed.status_code, listed.headers["Cache-Control"]), (503, "no-store"))

    def test_logs_never_contain_heart_rate_or_device_id(self) -> None:
        """ADR-044: ni frecuencias cardiacas ni identificadores de dispositivo en los logs."""
        self.reply = httpx.Response(200, json=BATCH_RESULT)
        self.client.post(MEASUREMENTS_PATH, json=BATCH, headers=self.auth())
        self.reply = httpx.Response(200, json=REVOKED)
        self.client.delete(f"/v1/me/devices/{DEVICE_ID}", headers=self.auth())
        self.reply = httpx.Response(500, json={"device_id": DEVICE_ID, "value": HEART_RATE})
        self.client.post(MEASUREMENTS_PATH, json=BATCH, headers=self.auth())

        logged = self.logs.text()
        self.assertIn("request.completed", logged, "la captura de logs funciona")
        self.assertIn("bff.upstream_unexpected_status", logged)
        self.assertNotIn(DEVICE_ID, logged)
        self.assertNotIn(str(HEART_RATE), logged)
        self.assertNotIn("999", logged)


if __name__ == "__main__":
    unittest.main()
