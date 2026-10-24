"""BFF web, HU017: asociaciones a servicios médicos deportivos (ADR-038).

Filtro de borde por actor y por la capacidad `services.sports_medicine.access` ante
Afiliación (`403 capability_required`, `503 upstream_unavailable`), reenvío literal del
cuerpo, la clave, los parámetros y la correlación, y respuestas del servicio sin alterar.
"""

import unittest
import uuid
from typing import Any

import httpx
from fastapi.testclient import TestClient
from test_training import Upstreams, token

from bff_web.main import create_app
from bff_web.settings import BffWebSettings
from sportapp_platform.correlation import CORRELATION_HEADER

ASSOCIATION = {
    "association_id": str(uuid.uuid4()),
    "service_id": str(uuid.uuid4()),
    "service": {
        "name": "Valoración médica deportiva",
        "category": "SPORTS_MEDICINE",
        "modality": "IN_PERSON",
        "fee": {"amount": "150000.00", "currency": "COP", "billing_unit": "SESSION"},
    },
    "status": "ACTIVE",
    "associated_at": "2026-10-24T15:00:00Z",
    "ended_at": None,
    "version": 1,
}
PREMIUM = {"subscription_status": "ACTIVE", "capabilities": ["services.sports_medicine.access"]}
PATH = "/v1/me/service-associations"


class ServiceAssociationRoutesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.upstreams = Upstreams()
        self.requests: list[httpx.Request] = []
        self.capability_requests: list[httpx.Request] = []
        self.response = httpx.Response(201, json=ASSOCIATION)
        self.snapshot: Any = httpx.Response(200, json=PREMIUM)
        self.services_down: type[Exception] | None = None

        def services(request: httpx.Request) -> httpx.Response:
            if self.services_down is not None:
                raise self.services_down("servicios")
            self.requests.append(request)
            return self.response

        def affiliation(request: httpx.Request) -> httpx.Response:
            self.capability_requests.append(request)
            if isinstance(self.snapshot, type):
                raise self.snapshot("afiliación")
            response: httpx.Response = self.snapshot
            return response

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
                affiliation_http=httpx.Client(
                    transport=httpx.MockTransport(affiliation), base_url="http://affiliation"
                ),
                services_http=httpx.Client(
                    transport=httpx.MockTransport(services), base_url="http://services"
                ),
            )
        )

    def auth(self, actor: str | None = "athlete", **headers: str) -> dict[str, str]:
        sent = dict(headers)
        if actor is not None:
            sent["Authorization"] = f"Bearer {token(actor)}"
        return sent

    def test_associate_forwards_body_key_and_correlation(self) -> None:
        """CA1 y CA3: cuerpo y clave tal cual; `Location` e `Idempotent-Replayed` se conservan."""
        correlation = str(uuid.uuid4())
        key = str(uuid.uuid4())
        location = f"{PATH}/{ASSOCIATION['association_id']}"
        self.response = httpx.Response(
            201, json=ASSOCIATION, headers={"Location": location, "Idempotent-Replayed": "true"}
        )

        response = self.client.post(
            PATH,
            json={"service_id": ASSOCIATION["service_id"], "extra": 1},
            headers=self.auth(**{"Idempotency-Key": key, CORRELATION_HEADER: correlation}),
        )

        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json(), ASSOCIATION)
        self.assertEqual(response.headers["Location"], location)
        self.assertEqual(response.headers["Idempotent-Replayed"], "true")
        [sent] = self.requests
        self.assertEqual((sent.method, sent.url.path), ("POST", PATH))
        self.assertEqual(
            sent.read(), b'{"service_id":"' + ASSOCIATION["service_id"].encode() + b'","extra":1}'
        )
        self.assertEqual(sent.headers["Idempotency-Key"], key)
        self.assertEqual(sent.headers[CORRELATION_HEADER], correlation)
        self.assertTrue(sent.headers["Authorization"].startswith("Bearer "))
        [capability] = self.capability_requests
        self.assertRegex(
            capability.url.path, r"^/v1/subscriptions/by-user/[0-9a-f-]{36}/capabilities$"
        )
        self.assertEqual(capability.headers[CORRELATION_HEADER], correlation)

    def test_dissociate_and_list(self) -> None:
        """CA5: `204` sin cuerpo; la lista reenvía solo los parámetros presentes."""
        association_id = uuid.uuid4()
        self.response = httpx.Response(204)
        deleted = self.client.delete(f"{PATH}/{association_id}", headers=self.auth())
        self.response = httpx.Response(200, json={"items": [ASSOCIATION], "next_cursor": None})
        listed = self.client.get(
            PATH, params={"status": "ALL", "limit": "5", "cursor": "abc"}, headers=self.auth()
        )
        self.client.get(PATH, headers=self.auth())

        self.assertEqual((deleted.status_code, deleted.content), (204, b""))
        self.assertEqual(self.requests[0].method, "DELETE")
        self.assertEqual(self.requests[0].url.path, f"{PATH}/{association_id}")
        self.assertEqual(listed.json(), {"items": [ASSOCIATION], "next_cursor": None})
        self.assertEqual(listed.headers["Cache-Control"], "no-store")
        self.assertEqual(
            dict(self.requests[1].url.params), {"status": "ALL", "limit": "5", "cursor": "abc"}
        )
        self.assertEqual(list(self.requests[2].url.params.multi_items()), [])

    def test_edge_rejections_do_not_reach_the_service(self) -> None:
        """CA2: sin capacidad `403 capability_required`; Afiliación caída o lenta `503`."""

        def every_operation(actor: str | None = "athlete") -> list[Any]:
            return [
                self.client.post(PATH, json={}, headers=self.auth(actor)),
                self.client.delete(f"{PATH}/{uuid.uuid4()}", headers=self.auth(actor)),
                self.client.get(PATH, headers=self.auth(actor)),
            ]

        outcomes: list[tuple[str, Any, int, str]] = []
        for label, snapshot, status, code in (
            (
                "FREE",
                httpx.Response(200, json={"subscription_status": "ACTIVE", "capabilities": []}),
                403,
                "capability_required",
            ),
            (
                "sin plan",
                httpx.Response(200, json={"subscription_status": "NONE", "capabilities": []}),
                403,
                "capability_required",
            ),
            ("caída", httpx.ConnectError, 503, "upstream_unavailable"),
            ("lenta", httpx.ReadTimeout, 503, "upstream_unavailable"),
            ("error", httpx.Response(500), 503, "upstream_unavailable"),
        ):
            self.snapshot = snapshot
            outcomes += [(label, response, status, code) for response in every_operation()]
        consulted = len(self.capability_requests)
        provider = every_operation("service_provider")
        anonymous = every_operation(None)

        for label, response, status, code in outcomes:
            with self.subTest(label=label):
                self.assertEqual((response.status_code, response.json()["code"]), (status, code))
        for response in provider:
            self.assertEqual(
                (response.status_code, response.json()["code"]), (403, "permission_required")
            )
        for response in anonymous:
            self.assertEqual(response.status_code, 401)
        self.assertEqual(
            len(self.capability_requests), consulted, "otro actor no consulta Afiliación"
        )
        self.assertEqual(self.requests, [])

    def test_service_errors_pass_through(self) -> None:
        error = {"code": "x", "message": "m", "correlation_id": None, "details": []}
        cases = [
            ("post", 404, "service_not_found"),
            ("post", 409, "association_exists"),
            ("post", 409, "idempotency_key_reused"),
            ("post", 422, "validation_error"),
            ("post", 403, "capability_required"),
            ("post", 503, "upstream_unavailable"),
            ("delete", 404, "association_not_found"),
            ("get", 422, "validation_error"),
        ]
        for method, status, code in cases:
            body = {
                **error,
                "code": code,
                "details": [{"field": "association_id", "issue": str(uuid.uuid4())}],
            }
            self.response = httpx.Response(status, json=body)
            with self.subTest(method=method, code=code):
                if method == "post":
                    response = self.client.post(PATH, json={}, headers=self.auth())
                elif method == "delete":
                    response = self.client.delete(f"{PATH}/{uuid.uuid4()}", headers=self.auth())
                else:
                    response = self.client.get(PATH, headers=self.auth())
                self.assertEqual((response.status_code, response.json()), (status, body))

    def test_unexpected_or_unavailable_service(self) -> None:
        self.response = httpx.Response(500, json={})
        unexpected = self.client.post(PATH, json={}, headers=self.auth())
        self.services_down = httpx.ConnectError
        down = self.client.get(PATH, headers=self.auth())
        self.services_down = httpx.ReadTimeout
        slow = self.client.delete(f"{PATH}/{uuid.uuid4()}", headers=self.auth())
        invalid_path = self.client.delete(f"{PATH}/x", headers=self.auth())

        self.assertEqual(
            (unexpected.status_code, unexpected.json()["code"]), (503, "upstream_unavailable")
        )
        self.assertEqual((down.status_code, down.json()["code"]), (503, "upstream_unavailable"))
        self.assertEqual((slow.status_code, slow.json()["code"]), (504, "upstream_timeout"))
        self.assertEqual(invalid_path.status_code, 422)


if __name__ == "__main__":
    unittest.main()
