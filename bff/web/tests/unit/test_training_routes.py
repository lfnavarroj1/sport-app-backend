"""BFF web, HU026 (ADR-041): filtro de borde (`organizer` con `training.routes.author` de
`actor-permissions.v2`) y reenvío de la publicación, el retiro y la lista de rutas a un
`training-management` simulado con `httpx.MockTransport`.

La aplicación se crea una vez por clase: cada `httpx.Client` real cuesta en Windows.
"""

import json
import unittest
import uuid
from typing import Any, ClassVar

import httpx
from fastapi.testclient import TestClient
from test_training import Upstreams, token

from bff_web.main import create_app
from bff_web.settings import BffWebSettings

ROUTE_ID = uuid.uuid4()


def route_request(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "name": "Vuelta al embalse",
        "description": None,
        "sport": "running",
        "distance_km": "12.5",
        "difficulty": "MODERATE",
        "city": "Medellín",
        "country_code": "CO",
        "start_point": {"lat": 6.2442, "lng": -75.5812},
        "waypoints": [{"lat": 6.25, "lng": -75.57}],
    }
    body.update(overrides)
    return body


def route(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        **route_request(),
        "route_id": str(ROUTE_ID),
        "organizer_id": str(uuid.uuid4()),
        "source": "ORGANIZER",
        "status": "PUBLISHED",
        "version": 1,
        "created_at": "2026-10-24T12:00:00Z",
        "updated_at": "2026-10-24T12:00:00Z",
        "published_at": "2026-10-24T12:00:00Z",
        "retired_at": None,
    }
    values.update(overrides)
    return values


def error(code: str, details: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {"code": code, "message": "m", "correlation_id": None, "details": details or []}


class TrainingRoutesTest(unittest.TestCase):
    client: ClassVar[TestClient]
    upstreams: ClassVar[Upstreams]

    @classmethod
    def setUpClass(cls) -> None:
        cls.upstreams = Upstreams()
        settings = BffWebSettings(
            app_env="test",  # type: ignore[arg-type]
            users_management_base_url="http://users.invalid",  # type: ignore[arg-type]
            affiliation_management_base_url="http://affiliation.invalid",  # type: ignore[arg-type]
            training_management_base_url="http://training.invalid",  # type: ignore[arg-type]
            complementary_services_base_url="http://services.invalid",  # type: ignore[arg-type]
            events_management_base_url="http://events.invalid",  # type: ignore[arg-type]
        )
        upstreams = cls.upstreams
        cls.client = TestClient(
            create_app(
                settings,
                users_http=httpx.Client(
                    transport=httpx.MockTransport(upstreams.users),
                    base_url="http://users",
                ),
                training_http=httpx.Client(
                    transport=httpx.MockTransport(upstreams.training),
                    base_url="http://training",
                ),
            )
        )

    def setUp(self) -> None:
        # Estado limpio de los servicios simulados en cada prueba.
        self.upstreams.training_requests.clear()
        self.upstreams.training_response = httpx.Response(201, json=route())
        self.upstreams.training_down = None

    def auth(self, actor: str | None = "organizer", **extra: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token(actor)}", **extra}

    def publish(self, body: dict[str, Any] | None = None, actor: str | None = "organizer") -> Any:
        return self.client.post(
            "/v1/routes",
            json=body or route_request(),
            headers=self.auth(actor, **{"Idempotency-Key": str(uuid.uuid4())}),
        )

    def test_publish_forwards_body_key_and_token_and_relays_headers(self) -> None:
        """CA1: el servicio recibe el cuerpo, la clave y el token; `201` sin alterar."""
        published = route()
        self.upstreams.training_response = httpx.Response(
            201,
            json=published,
            headers={"Location": f"/v1/routes/{ROUTE_ID}", "Idempotent-Replayed": "true"},
        )
        key, raw = str(uuid.uuid4()), token("organizer")

        response = self.client.post(
            "/v1/routes",
            json=route_request(),
            headers={"Authorization": f"Bearer {raw}", "Idempotency-Key": key},
        )

        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json(), published)
        self.assertEqual(response.headers["Location"], f"/v1/routes/{ROUTE_ID}")
        self.assertEqual(response.headers["Idempotent-Replayed"], "true")
        [forwarded] = self.upstreams.training_requests
        self.assertEqual((forwarded.method, forwarded.url.path), ("POST", "/v1/routes"))
        self.assertEqual(forwarded.headers["Idempotency-Key"], key)
        self.assertEqual(forwarded.headers["Authorization"], f"Bearer {raw}")
        self.assertEqual(json.loads(forwarded.content), route_request())

    def test_only_route_authors_reach_the_service(self) -> None:
        """`actor-permissions.v2`: solo el organizador web tiene `training.routes.author`."""
        for actor in ("athlete", "service_provider", None):
            with self.subTest(actor=actor):
                responses = (
                    self.publish(actor=actor),
                    self.client.post(
                        f"/v1/routes/{ROUTE_ID}/retire",
                        json={"expected_version": 1},
                        headers=self.auth(actor),
                    ),
                    self.client.get("/v1/me/routes", headers=self.auth(actor)),
                )
                for response in responses:
                    self.assertEqual(
                        (response.status_code, response.json()["code"]),
                        (403, "permission_required"),
                    )
        unauthenticated = self.client.get("/v1/me/routes")
        self.assertEqual(unauthenticated.status_code, 401)
        self.assertEqual(self.upstreams.training_requests, [])

    def test_shape_errors_are_rejected_at_the_edge(self) -> None:
        """CA2: la forma del cuerpo se valida como en el servicio; no se reenvía."""
        for body in (
            route_request(start_point={"lat": 91, "lng": 0}),
            {k: v for k, v in route_request().items() if k != "name"},
            route_request(gpx="<gpx/>"),
        ):
            with self.subTest(body=body):
                self.assertEqual(self.publish(body).status_code, 422)
        self.assertEqual(self.upstreams.training_requests, [])

    def test_service_errors_are_relayed_unchanged(self) -> None:
        """CA2 y CA4: `422` de reglas, `404` de ruta ajena y `409` de versión, sin alterar."""
        cases = (
            (
                "post",
                "/v1/routes",
                422,
                error("validation_error", [{"field": "body.distance_km", "issue": "out_of_range"}]),
            ),
            ("post", "/v1/routes", 409, error("idempotency_key_reused")),
            ("retire", f"/v1/routes/{ROUTE_ID}/retire", 404, error("route_not_found")),
            (
                "retire",
                f"/v1/routes/{ROUTE_ID}/retire",
                409,
                error("version_conflict", [{"field": "expected_version", "issue": "2"}]),
            ),
            ("list", "/v1/me/routes", 422, error("validation_error")),
        )
        for kind, path, status, body in cases:
            with self.subTest(path=path, status=status):
                self.upstreams.training_response = httpx.Response(status, json=body)
                if kind == "post":
                    response = self.publish()
                elif kind == "retire":
                    response = self.client.post(
                        path, json={"expected_version": 1}, headers=self.auth()
                    )
                else:
                    response = self.client.get(path, params={"limit": "0"}, headers=self.auth())

                self.assertEqual((response.status_code, response.json()), (status, body))

    def test_retire_and_list_forward_version_and_query(self) -> None:
        retired = route(status="RETIRED", version=2, retired_at="2026-10-25T12:00:00Z")
        self.upstreams.training_response = httpx.Response(200, json=retired)

        response = self.client.post(
            f"/v1/routes/{ROUTE_ID}/retire", json={"expected_version": 1}, headers=self.auth()
        )

        self.assertEqual((response.status_code, response.json()), (200, retired))
        [forwarded] = self.upstreams.training_requests
        self.assertEqual(forwarded.url.path, f"/v1/routes/{ROUTE_ID}/retire")
        self.assertEqual(json.loads(forwarded.content), {"expected_version": 1})

        page = {"items": [retired], "next_cursor": None}
        self.upstreams.training_response = httpx.Response(200, json=page)
        listed = self.client.get(
            "/v1/me/routes", params={"limit": "5", "cursor": "abc"}, headers=self.auth()
        )

        self.assertEqual((listed.status_code, listed.json()), (200, page))
        self.assertEqual(listed.headers["Cache-Control"], "no-store")
        self.assertEqual(
            dict(self.upstreams.training_requests[-1].url.params), {"limit": "5", "cursor": "abc"}
        )

    def test_training_down_or_unexpected_is_503_or_504(self) -> None:
        self.upstreams.training_down = httpx.ConnectTimeout
        self.assertEqual(self.publish().status_code, 504)
        self.upstreams.training_down = httpx.ConnectError
        self.assertEqual(self.client.get("/v1/me/routes", headers=self.auth()).status_code, 503)
        self.upstreams.training_down = None
        self.upstreams.training_response = httpx.Response(500, json=error("x"))
        self.assertEqual(self.publish().status_code, 503)


if __name__ == "__main__":
    unittest.main()
