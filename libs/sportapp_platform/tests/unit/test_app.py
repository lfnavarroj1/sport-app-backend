import unittest
import uuid

from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from sportapp_platform.app import create_service_app
from sportapp_platform.config import ServiceSettings
from sportapp_platform.correlation import CORRELATION_HEADER


class _Check:
    def __init__(self, name: str, result: bool | Exception) -> None:
        self.name = name
        self._result = result

    def __call__(self) -> bool:
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


class _Payload(BaseModel):
    quantity: int


def _technical_router() -> APIRouter:
    router = APIRouter(prefix="/technical-probe")

    @router.post("/validate")
    def validate(payload: _Payload) -> dict[str, int]:
        return {"quantity": payload.quantity}

    @router.get("/fail")
    def fail() -> None:
        raise RuntimeError("detalle interno que no debe salir")

    return router


def _app(*checks: _Check, environment: str = "test") -> FastAPI:
    return create_service_app(
        service_name="probe",
        title="Probe",
        api_version="0.1.0",
        settings=ServiceSettings(app_env=environment),  # type: ignore[arg-type]
        readiness_checks=checks,
        routers=[_technical_router()],
    )


class HealthTest(unittest.TestCase):
    def test_live_does_not_query_dependencies(self) -> None:
        client = TestClient(_app(_Check("database", False)))

        response = client.get("/health/live")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok", "checks": {}})

    def test_ready_when_all_checks_pass(self) -> None:
        client = TestClient(_app(_Check("database", True)))

        response = client.get("/health/ready")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok", "checks": {"database": "ok"}})

    def test_not_ready_when_a_check_fails_or_raises(self) -> None:
        client = TestClient(_app(_Check("database", False), _Check("broker", RuntimeError())))

        response = client.get("/health/ready")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.json(),
            {"status": "unavailable", "checks": {"database": "failed", "broker": "failed"}},
        )


class CorrelationTest(unittest.TestCase):
    def test_propagates_valid_correlation_id(self) -> None:
        correlation_id = str(uuid.uuid4())
        client = TestClient(_app())

        response = client.get("/health/live", headers={CORRELATION_HEADER: correlation_id})

        self.assertEqual(response.headers[CORRELATION_HEADER], correlation_id)

    def test_replaces_invalid_correlation_id(self) -> None:
        client = TestClient(_app())

        response = client.get("/health/live", headers={CORRELATION_HEADER: "x\nlog-forjado"})

        generated = response.headers[CORRELATION_HEADER]
        self.assertEqual(str(uuid.UUID(generated)), generated)


class ErrorFormatTest(unittest.TestCase):
    def test_not_found_uses_common_format(self) -> None:
        client = TestClient(_app())

        response = client.get("/no-existe")

        body = response.json()
        self.assertEqual(response.status_code, 404)
        self.assertEqual(body["code"], "not_found")
        self.assertEqual(body["correlation_id"], response.headers[CORRELATION_HEADER])
        self.assertEqual(body["details"], [])

    def test_validation_error_does_not_echo_input(self) -> None:
        client = TestClient(_app())

        response = client.post("/technical-probe/validate", json={"quantity": "dato-sensible"})

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["code"], "validation_error")
        self.assertEqual(
            response.json()["details"], [{"field": "body.quantity", "issue": "int_parsing"}]
        )
        self.assertNotIn("dato-sensible", response.text)

    def test_unexpected_error_hides_internal_detail(self) -> None:
        client = TestClient(_app(), raise_server_exceptions=False)

        with self.assertLogs("sportapp.errors", level="ERROR"):
            response = client.get("/technical-probe/fail")

        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["code"], "internal_error")
        self.assertNotIn("detalle interno", response.text)

    def test_method_not_allowed_and_generic_http_errors(self) -> None:
        client = TestClient(_app())

        response = client.delete("/health/live")

        self.assertEqual(response.status_code, 405)
        self.assertEqual(response.json()["code"], "method_not_allowed")


class DocumentationExposureTest(unittest.TestCase):
    def test_production_does_not_expose_openapi_or_docs(self) -> None:
        client = TestClient(_app(environment="production"))

        self.assertEqual(client.get("/openapi.json").status_code, 404)
        self.assertEqual(client.get("/docs").status_code, 404)

    def test_non_production_exposes_openapi(self) -> None:
        client = TestClient(_app())

        self.assertEqual(client.get("/openapi.json").status_code, 200)


class ShutdownTest(unittest.TestCase):
    def test_runs_shutdown_hooks(self) -> None:
        calls: list[str] = []
        app = create_service_app(
            service_name="probe",
            title="Probe",
            api_version="0.1.0",
            settings=ServiceSettings(app_env="test"),
            shutdown_hooks=[lambda: calls.append("disposed")],
        )

        with TestClient(app):
            pass

        self.assertEqual(calls, ["disposed"])


if __name__ == "__main__":
    unittest.main()
