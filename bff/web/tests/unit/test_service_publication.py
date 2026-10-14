"""BFF web, HU019: publicar y consultar servicios propios (ADR-027 y ADR-038).

Filtro de borde, reenvío literal de clave, parámetros y correlación, y errores del
servicio sin alterar.
"""

import unittest
import uuid
from collections.abc import Callable
from typing import Any

import httpx
from fastapi.testclient import TestClient
from test_training import Upstreams, token

from bff_web.main import create_app
from bff_web.settings import BffWebSettings
from sportapp_platform.correlation import CORRELATION_HEADER

SERVICE_ID = uuid.uuid4()
PUBLISHED = {
    "service_id": str(SERVICE_ID),
    "provider_id": str(uuid.uuid4()),
    "status": "PUBLISHED",
    "version": 2,
    "published_at": "2026-10-13T15:00:00Z",
    "updated_at": "2026-10-13T15:00:00Z",
}


def error(code: str, details: list[dict[str, str]] | None = None) -> dict[str, Any]:
    return {"code": code, "message": "m", "correlation_id": None, "details": details or []}


class ServicePublicationRoutesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.upstreams = Upstreams()
        self.requests: list[httpx.Request] = []
        self.response = httpx.Response(200, json=PUBLISHED)
        self.failure: type[Exception] | None = None

        def services(request: httpx.Request) -> httpx.Response:
            if self.failure is not None:
                raise self.failure("servicios")
            self.requests.append(request)
            return self.response

        self.client = TestClient(
            create_app(
                BffWebSettings(
                    app_env="test",  # type: ignore[arg-type]
                    users_management_base_url="http://users.invalid",  # type: ignore[arg-type]
                    affiliation_management_base_url="http://affiliation.invalid",  # type: ignore[arg-type]
                    training_management_base_url="http://training.invalid",  # type: ignore[arg-type]
                    complementary_services_base_url="http://services.invalid",  # type: ignore[arg-type]
                    events_management_base_url="http://events.invalid",  # type: ignore[arg-type]
                ),
                users_http=httpx.Client(
                    transport=httpx.MockTransport(self.upstreams.users), base_url="http://users"
                ),
                services_http=httpx.Client(
                    transport=httpx.MockTransport(services), base_url="http://services"
                ),
            )
        )

    def headers(self, actor: str | None, **extra: str) -> dict[str, str]:
        sent = dict(extra)
        if actor is not None:
            sent["Authorization"] = f"Bearer {token(actor)}"
        return sent

    def publish(self, actor: str | None = "service_provider", **headers: str) -> Any:
        return self.client.post(
            f"/v1/services/{SERVICE_ID}/publish", headers=self.headers(actor, **headers)
        )

    def list_mine(
        self, actor: str | None = "service_provider", params: dict[str, str] | None = None
    ) -> Any:
        return self.client.get("/v1/me/services", params=params, headers=self.headers(actor))

    def get_mine(self, actor: str | None = "service_provider") -> Any:
        return self.client.get(f"/v1/me/services/{SERVICE_ID}", headers=self.headers(actor))

    # --- POST /v1/services/{service_id}/publish ---

    def test_publish_forwards_key_token_and_correlation(self) -> None:
        key = str(uuid.uuid4())
        correlation_id = str(uuid.uuid4())
        self.response = httpx.Response(200, json=PUBLISHED, headers={"Idempotent-Replayed": "true"})

        response = self.publish(**{"Idempotency-Key": key, CORRELATION_HEADER: correlation_id})

        self.assertEqual((response.status_code, response.json()), (200, PUBLISHED))
        self.assertEqual(response.headers["Idempotent-Replayed"], "true")
        [sent] = self.requests
        self.assertEqual(sent.method, "POST")
        self.assertEqual(sent.url.path, f"/v1/services/{SERVICE_ID}/publish")
        self.assertEqual(sent.content, b"")
        self.assertEqual(sent.headers["Idempotency-Key"], key)
        self.assertEqual(sent.headers[CORRELATION_HEADER], correlation_id)
        self.assertTrue(sent.headers["Authorization"].startswith("Bearer "))

    def test_publish_without_replay_header_does_not_add_it(self) -> None:
        response = self.publish(**{"Idempotency-Key": str(uuid.uuid4())})

        self.assertNotIn("Idempotent-Replayed", response.headers)

    def test_publish_missing_key_is_not_invented(self) -> None:
        """Si falta la clave, el servicio responde el `422`; el BFF no la genera."""
        payload = error("validation_error", [{"field": "Idempotency-Key", "issue": "required"}])
        self.response = httpx.Response(422, json=payload)

        response = self.publish()

        self.assertEqual((response.status_code, response.json()), (422, payload))
        self.assertNotIn("Idempotency-Key", self.requests[0].headers)

    def test_publish_edge_permission(self) -> None:
        """Solo `service_provider` tiene `services.publish` en web."""
        for actor in ("athlete", "organizer"):
            with self.subTest(actor=actor):
                response = self.publish(actor, **{"Idempotency-Key": str(uuid.uuid4())})
                self.assertEqual(
                    (response.status_code, response.json()["code"]), (403, "permission_required")
                )
        self.assertEqual(self.publish(None).status_code, 401)
        self.assertEqual(self.requests, [])

    def test_publish_service_errors_pass_through(self) -> None:
        for status, payload in (
            (404, error("service_not_found")),
            (409, error("idempotency_key_reused")),
            (422, error("validation_error", [{"field": "availability", "issue": "required"}])),
            (403, error("permission_required")),
            (401, error("invalid_access_token")),
        ):
            with self.subTest(status=status):
                self.response = httpx.Response(status, json=payload)
                response = self.publish(**{"Idempotency-Key": str(uuid.uuid4())})
                self.assertEqual((response.status_code, response.json()), (status, payload))

    def test_publish_rejects_malformed_service_id_without_calling(self) -> None:
        response = self.client.post(
            "/v1/services/no-es-uuid/publish", headers=self.headers("service_provider")
        )

        self.assertEqual((response.status_code, response.json()["code"]), (422, "validation_error"))
        self.assertEqual(self.requests, [])

    # --- GET /v1/me/services ---

    def test_list_forwards_present_params_verbatim(self) -> None:
        page = {"items": [], "next_cursor": None}
        self.response = httpx.Response(200, json=page)

        response = self.list_mine(params={"status": "DRAFT", "limit": "5", "cursor": "abc"})

        self.assertEqual((response.status_code, response.json()), (200, page))
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        [sent] = self.requests
        self.assertEqual(sent.url.path, "/v1/me/services")
        self.assertEqual(dict(sent.url.params), {"status": "DRAFT", "limit": "5", "cursor": "abc"})
        self.assertTrue(sent.headers["Authorization"].startswith("Bearer "))

    def test_list_forwards_only_present_and_invalid_params(self) -> None:
        """Los valores inválidos los valida el servicio; el BFF no aplica valores por defecto."""
        self.response = httpx.Response(200, json={"items": [], "next_cursor": None})

        self.list_mine()
        self.list_mine(params={"limit": "0"})

        self.assertEqual(dict(self.requests[0].url.params), {})
        self.assertEqual(dict(self.requests[1].url.params), {"limit": "0"})

    def test_list_service_validation_error_passes_through(self) -> None:
        payload = error("validation_error", [{"field": "query.limit", "issue": "out_of_range"}])
        self.response = httpx.Response(422, json=payload)

        response = self.list_mine(params={"limit": "500"})

        self.assertEqual((response.status_code, response.json()), (422, payload))

    # --- GET /v1/me/services/{service_id} ---

    def test_detail_forwards_and_returns_service(self) -> None:
        service = {**PUBLISHED, "name": "Valoración", "published_at": None, "status": "DRAFT"}
        self.response = httpx.Response(200, json=service)

        response = self.get_mine()

        self.assertEqual((response.status_code, response.json()), (200, service))
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(self.requests[0].url.path, f"/v1/me/services/{SERVICE_ID}")

    def test_detail_not_found_passes_through(self) -> None:
        payload = error("service_not_found")
        self.response = httpx.Response(404, json=payload)

        response = self.get_mine()

        self.assertEqual((response.status_code, response.json()), (404, payload))

    # --- Comunes a las consultas ---

    def test_queries_edge_actor(self) -> None:
        """Otro actor recibe `403 permission_required` sin llamar al servicio."""
        for name, call in (("list", self.list_mine), ("detail", self.get_mine)):
            for actor in ("athlete", "organizer"):
                with self.subTest(route=name, actor=actor):
                    response = call(actor)
                    self.assertEqual(
                        (response.status_code, response.json()["code"]),
                        (403, "permission_required"),
                    )
            with self.subTest(route=name, actor=None):
                self.assertEqual(call(None).status_code, 401)
        self.assertEqual(self.requests, [])

    def test_failures(self) -> None:
        calls: tuple[tuple[str, Callable[[], Any]], ...] = (
            ("publish", lambda: self.publish(**{"Idempotency-Key": str(uuid.uuid4())})),
            ("list", self.list_mine),
            ("detail", self.get_mine),
        )
        for name, call in calls:
            with self.subTest(route=name):
                self.failure = httpx.ReadTimeout
                timeout = call()
                self.failure = httpx.ConnectError
                down = call()
                self.failure = None
                self.response = httpx.Response(500)
                with self.assertLogs("sportapp.bff", level="WARNING") as logs:
                    unexpected = call()

                self.assertEqual(
                    (timeout.status_code, timeout.json()["code"]), (504, "upstream_timeout")
                )
                self.assertEqual(
                    (down.status_code, down.json()["code"]), (503, "upstream_unavailable")
                )
                self.assertEqual(
                    (unexpected.status_code, unexpected.json()["code"]),
                    (503, "upstream_unavailable"),
                )
                self.assertIn("bff.upstream_unexpected_status", logs.output[0])


if __name__ == "__main__":
    unittest.main()
