"""BFF web, HU014 (ADR-037): filtro de borde, resolución del correo del deportista y reenvío
de los planes de alimentación y del catálogo de alimentos a un `training-management` simulado.

Usuarios (JWKS y lookup) y Entrenamientos se simulan con `httpx.MockTransport`.
"""

import json
import unittest
import uuid
from typing import Any

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient
from test_training import ATHLETE_ID, PROFESSIONAL_ID, Upstreams, token

from bff_web.main import create_app
from bff_web.settings import BffWebSettings

PLAN_ID = uuid.uuid4()
ETAG = '"food-catalog-2026-10-13.v1"'


def plan_request(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "athlete_email": "ana@example.com",
        "title": "Plan de recuperación",
        "objectives": ["Recuperar glucógeno"],
        "start_date": "2026-11-02",
        "end_date": "2026-11-29",
        "meals": [
            {
                "meal_type": "BREAKFAST",
                "items": [
                    {"food_code": "OATS", "quantity": "60.0", "unit": "G"},
                    {"food_code": "MILK", "quantity": "250", "unit": "ML"},
                ],
            },
            {
                "meal_type": "HYDRATION",
                "items": [{"food_code": "WATER", "quantity": "500.0", "unit": "ML"}],
            },
        ],
    }
    values.update(overrides)
    return values


def plan(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "plan_id": str(PLAN_ID),
        "athlete_id": str(ATHLETE_ID),
        "professional_id": str(PROFESSIONAL_ID),
        "title": "Plan de recuperación",
        "objectives": ["Recuperar glucógeno"],
        "start_date": "2026-11-02",
        "end_date": "2026-11-29",
        "status": "ASSIGNED",
        "version": 1,
        "catalog_version": "food-catalog-2026-10-13.v1",
        "meals": [
            {
                "meal_type": "BREAKFAST",
                "items": [{"food_code": "OATS", "quantity": "60.0", "unit": "G"}],
            }
        ],
        "created_at": "2026-10-22T12:00:00Z",
    }
    values.update(overrides)
    return values


def error(code: str, details: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {"code": code, "message": "m", "correlation_id": None, "details": details or []}


CATALOG = {
    "catalog_version": "food-catalog-2026-10-13.v1",
    "items": [
        {
            "food_code": "OATS",
            "label_key": "food.oats",
            "category": "GRAIN",
            "default_unit": "G",
            "allowed_units": ["G", "CUP"],
            "allergens": [],
            "incompatible_with": [],
        }
    ],
}


class NutritionRoutesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.upstreams = Upstreams()
        settings = BffWebSettings(
            app_env="test",  # type: ignore[arg-type]
            users_management_base_url="http://users.invalid",  # type: ignore[arg-type]
            affiliation_management_base_url="http://affiliation.invalid",  # type: ignore[arg-type]
            training_management_base_url="http://training.invalid",  # type: ignore[arg-type]
            complementary_services_base_url="http://services.invalid",  # type: ignore[arg-type]
            events_management_base_url="http://events.invalid",  # type: ignore[arg-type]
        )
        self.client = TestClient(
            create_app(
                settings,
                users_http=httpx.Client(
                    transport=httpx.MockTransport(self.upstreams.users), base_url="http://users"
                ),
                training_http=httpx.Client(
                    transport=httpx.MockTransport(self.upstreams.training),
                    base_url="http://training",
                ),
            )
        )

    def auth(self, actor: str | None = "service_provider", **extra: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token(actor)}", **extra}

    def create(
        self,
        body: dict[str, Any] | None = None,
        actor: str | None = "service_provider",
        key: str | None = None,
    ) -> Any:
        return self.client.post(
            "/v1/nutrition-plans",
            json=body or plan_request(),
            headers=self.auth(actor, **{"Idempotency-Key": key or str(uuid.uuid4())}),
        )

    def get_plan(self, actor: str | None = "athlete") -> Any:
        return self.client.get(f"/v1/nutrition-plans/{PLAN_ID}", headers=self.auth(actor))

    def my_plans(self, actor: str | None = "athlete", **params: str) -> Any:
        return self.client.get("/v1/me/nutrition-plans", params=params, headers=self.auth(actor))

    def catalog(self, actor: str | None = "athlete", **extra: str) -> Any:
        return self.client.get("/v1/food-catalog", headers=self.auth(actor, **extra))

    # --- POST /v1/nutrition-plans -------------------------------------------------------

    def test_create_resolves_athlete_and_forwards_body_key_and_token(self) -> None:
        """CA1: el servicio recibe `athlete_id` (nunca el correo), la clave y el token."""
        created = plan(warnings=["nutrition_habits_missing"])
        self.upstreams.training_response = httpx.Response(
            201,
            json=created,
            headers={"Location": f"/v1/nutrition-plans/{PLAN_ID}", "Idempotent-Replayed": "true"},
        )
        key = str(uuid.uuid4())
        raw_token = token("service_provider")

        response = self.client.post(
            "/v1/nutrition-plans",
            json=plan_request(),
            headers={"Authorization": f"Bearer {raw_token}", "Idempotency-Key": key},
        )

        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json(), created)
        self.assertEqual(response.headers["Location"], f"/v1/nutrition-plans/{PLAN_ID}")
        self.assertEqual(response.headers["Idempotent-Replayed"], "true")
        [forwarded] = self.upstreams.training_requests
        self.assertEqual((forwarded.method, forwarded.url.path), ("POST", "/v1/nutrition-plans"))
        self.assertEqual(forwarded.headers["Idempotency-Key"], key)
        self.assertEqual(forwarded.headers["Authorization"], f"Bearer {raw_token}")
        sent = json.loads(forwarded.content)
        expected = {k: v for k, v in plan_request().items() if k != "athlete_email"}
        self.assertEqual(sent, expected | {"athlete_id": str(ATHLETE_ID)})
        self.assertNotIn("athlete_email", sent)

    def test_create_trims_title_and_objectives_like_the_service(self) -> None:
        self.upstreams.training_response = httpx.Response(201, json=plan(warnings=[]))

        self.create(plan_request(title="  Plan  ", objectives=["  Hidratarme  "]))

        sent = json.loads(self.upstreams.training_requests[0].content)
        self.assertEqual((sent["title"], sent["objectives"]), ("Plan", ["Hidratarme"]))

    def test_unknown_or_non_athlete_email_is_indistinguishable_from_missing_authorization(
        self,
    ) -> None:
        """CA3 sin enumeración: correo inexistente o de otro actor → mismo 403, sin servicio."""
        for email in ("nadie@example.com", "pro@example.com", "org@example.com"):
            with self.subTest(email=email):
                response = self.create(plan_request(athlete_email=email))
                self.assertEqual(response.status_code, 403)
                self.assertEqual(response.json()["code"], "athlete_authorization_required")
        self.assertEqual(self.upstreams.training_requests, [])

    def test_create_edge_filter_requires_nutrition_author_permission(self) -> None:
        """CA3: deportista, organizador y token sin actor no tienen `nutrition.plan.author`."""
        for actor in ("athlete", "organizer", None):
            with self.subTest(actor=actor):
                response = self.create(actor=actor)
                self.assertEqual(
                    (response.status_code, response.json()["code"]), (403, "permission_required")
                )
        self.assertEqual(self.upstreams.training_requests, [])

    def test_create_requires_uuid_idempotency_key(self) -> None:
        without = self.client.post("/v1/nutrition-plans", json=plan_request(), headers=self.auth())
        invalid = self.create(key="no-es-uuid")

        self.assertEqual((without.status_code, invalid.status_code), (422, 422))
        self.assertEqual(self.upstreams.training_requests, [])

    def test_create_transport_validation_happens_in_the_bff(self) -> None:
        for body in (
            plan_request(meals=[]),
            plan_request(extra="x"),
            plan_request(athlete_email="no-es-correo"),
            plan_request(
                meals=[
                    {
                        "meal_type": "BRUNCH",
                        "items": [{"food_code": "OATS", "quantity": "1.0", "unit": "G"}],
                    }
                ]
            ),
            plan_request(
                meals=[
                    {
                        "meal_type": "LUNCH",
                        "items": [{"food_code": "OATS", "quantity": "1.0", "unit": "KG"}],
                    }
                ]
            ),
            plan_request(
                meals=[
                    {
                        "meal_type": "LUNCH",
                        "items": [{"food_code": "OATS", "quantity": "1,5", "unit": "G"}],
                    }
                ]
            ),
            plan_request(objectives=["x"] * 6),
        ):
            with self.subTest(body=body):
                self.assertEqual(self.create(body).status_code, 422)
        self.assertEqual(self.upstreams.training_requests, [])

    def test_create_service_errors_pass_through_unaltered(self) -> None:
        """CA2/CA6: bloqueos, reglas, clave reutilizada y Usuarios caído llegan tal cual."""
        blocked = error(
            "nutrition_plan_invalid",
            [{"field": "body.meals[0].items[1].food_code", "issue": "conflicts_with_allergy"}],
        )
        for status, body in (
            (403, error("athlete_authorization_required")),
            (409, error("idempotency_key_reused")),
            (422, blocked),
            (503, error("upstream_unavailable")),
        ):
            with self.subTest(status=status):
                self.upstreams.training_response = httpx.Response(status, json=body)
                response = self.create()
                self.assertEqual((response.status_code, response.json()), (status, body))

    def test_create_upstream_failures(self) -> None:
        self.upstreams.lookup_down = True
        lookup_down = self.create()
        self.upstreams.lookup_down = False
        self.upstreams.training_down = httpx.ReadTimeout
        timeout = self.create()
        self.upstreams.training_down = httpx.ConnectError
        unavailable = self.create()
        self.upstreams.training_down = None
        self.upstreams.training_response = httpx.Response(500)
        with self.assertLogs("sportapp.bff", level="WARNING"):
            unexpected = self.create()

        self.assertEqual(
            (lookup_down.status_code, lookup_down.json()["code"]), (503, "upstream_unavailable")
        )
        self.assertEqual((timeout.status_code, timeout.json()["code"]), (504, "upstream_timeout"))
        self.assertEqual(unavailable.status_code, 503)
        self.assertEqual(unexpected.status_code, 503)

    # --- GET /v1/nutrition-plans/{plan_id} ---------------------------------------------

    def test_get_plan_forwards_athlete_and_author_and_disables_cache(self) -> None:
        """El servicio decide la propiedad (creador o deportista dueño)."""
        self.upstreams.training_response = httpx.Response(200, json=plan())
        for actor in ("athlete", "service_provider"):
            with self.subTest(actor=actor):
                response = self.get_plan(actor)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json(), plan())
                self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(
            {r.url.path for r in self.upstreams.training_requests},
            {f"/v1/nutrition-plans/{PLAN_ID}"},
        )

    def test_get_plan_edge_filter(self) -> None:
        """CA3 en el borde: un actor sin rol de deportista ni permiso de autor no llega."""
        for actor in ("organizer", None):
            with self.subTest(actor=actor):
                response = self.get_plan(actor)
                self.assertEqual(
                    (response.status_code, response.json()["code"]), (403, "permission_required")
                )
        self.assertEqual(self.upstreams.training_requests, [])

    def test_get_plan_service_errors_pass_through(self) -> None:
        """CA3: otro actor o autorización revocada → 403; tercero o sin proyectar → 404."""
        for status, code in (
            (403, "permission_required"),
            (403, "athlete_authorization_required"),
            (404, "nutrition_plan_not_found"),
        ):
            with self.subTest(code=code):
                self.upstreams.training_response = httpx.Response(status, json=error(code))
                response = self.get_plan()
                self.assertEqual((response.status_code, response.json()["code"]), (status, code))
                self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_get_plan_rejects_non_uuid_and_upstream_failures(self) -> None:
        malformed = self.client.get("/v1/nutrition-plans/abc", headers=self.auth())
        self.upstreams.training_down = httpx.ReadTimeout
        timeout = self.get_plan()
        self.upstreams.training_down = None
        self.upstreams.training_response = httpx.Response(503, json=error("upstream_unavailable"))
        with self.assertLogs("sportapp.bff", level="WARNING"):
            unexpected = self.get_plan()

        self.assertEqual(malformed.status_code, 422)
        self.assertEqual(timeout.status_code, 504)
        self.assertEqual(unexpected.status_code, 503)

    # --- GET /v1/me/nutrition-plans ------------------------------------------------------

    def test_my_plans_forwards_parameters_and_disables_cache(self) -> None:
        page = {"items": [plan()], "next_cursor": "c2"}
        self.upstreams.training_response = httpx.Response(200, json=page)

        default = self.my_plans()
        explicit = self.my_plans(limit="50", cursor="c1")

        self.assertEqual((default.status_code, default.json()), (200, page))
        self.assertEqual(explicit.headers["Cache-Control"], "no-store")
        first, second = self.upstreams.training_requests
        self.assertEqual(first.url.path, "/v1/me/nutrition-plans")
        self.assertEqual(dict(first.url.params), {}, "el servicio aplica el límite por defecto")
        self.assertEqual(dict(second.url.params), {"limit": "50", "cursor": "c1"})

    def test_my_plans_only_for_athletes_at_the_edge(self) -> None:
        for actor in ("service_provider", "organizer", None):
            with self.subTest(actor=actor):
                response = self.my_plans(actor)
                self.assertEqual(
                    (response.status_code, response.json()["code"]), (403, "permission_required")
                )
        self.assertEqual(self.upstreams.training_requests, [])

    def test_my_plans_parameter_limits(self) -> None:
        """`limit` lo valida el servicio (`out_of_range`, como HU034): el BFF lo reenvía tal
        cual; solo corta un cursor de más de 512 caracteres."""
        self.assertEqual(self.my_plans(cursor="c" * 513).status_code, 422)
        self.assertEqual(self.upstreams.training_requests, [])
        for raw in ("0", "51", "x"):
            with self.subTest(limit=raw):
                self.my_plans(limit=raw)
                self.assertEqual(self.upstreams.training_requests[-1].url.params["limit"], raw)

    def test_my_plans_service_errors(self) -> None:
        for status, expected in ((422, 422), (403, 403), (500, 503)):
            with self.subTest(status=status):
                self.upstreams.training_response = httpx.Response(
                    status, json=error("invalid_cursor")
                )
                if status == 500:
                    with self.assertLogs("sportapp.bff", level="WARNING"):
                        response = self.my_plans(cursor="x")
                else:
                    response = self.my_plans(cursor="x")
                self.assertEqual(response.status_code, expected)
                self.assertEqual(response.headers["Cache-Control"], "no-store")

    # --- GET /v1/food-catalog -------------------------------------------------------------

    def test_catalog_returns_body_and_etag_for_any_actor(self) -> None:
        self.upstreams.training_response = httpx.Response(200, json=CATALOG, headers={"ETag": ETAG})
        for actor in ("athlete", "service_provider", "organizer"):
            with self.subTest(actor=actor):
                response = self.catalog(actor)
                self.assertEqual((response.status_code, response.json()), (200, CATALOG))
                self.assertEqual(response.headers["ETag"], ETAG)
        self.assertNotIn("If-None-Match", self.upstreams.training_requests[0].headers)

    def test_catalog_forwards_if_none_match_and_relays_304_without_body(self) -> None:
        self.upstreams.training_response = httpx.Response(304, headers={"ETag": ETAG})

        response = self.catalog(**{"If-None-Match": ETAG})

        self.assertEqual(response.status_code, 304)
        self.assertEqual(response.content, b"")
        self.assertEqual(response.headers["ETag"], ETAG)
        [forwarded] = self.upstreams.training_requests
        self.assertEqual(forwarded.url.path, "/v1/food-catalog")
        self.assertEqual(forwarded.headers["If-None-Match"], ETAG)

    def test_catalog_upstream_failures(self) -> None:
        self.upstreams.training_down = httpx.ReadTimeout
        timeout = self.catalog()
        self.upstreams.training_down = httpx.ConnectError
        unavailable = self.catalog()
        self.upstreams.training_down = None
        self.upstreams.training_response = httpx.Response(500)
        with self.assertLogs("sportapp.bff", level="WARNING"):
            unexpected = self.catalog()

        self.assertEqual(timeout.status_code, 504)
        self.assertEqual(unavailable.status_code, 503)
        self.assertEqual(unexpected.status_code, 503)

    # --- Autenticación -------------------------------------------------------------------

    def test_all_operations_require_a_valid_token(self) -> None:
        other = Ed25519PrivateKey.generate()
        forged = {"Authorization": f"Bearer {token('service_provider', other)}"}
        calls = (
            lambda h: self.client.post(
                "/v1/nutrition-plans",
                json=plan_request(),
                headers={**h, "Idempotency-Key": str(uuid.uuid4())},
            ),
            lambda h: self.client.get(f"/v1/nutrition-plans/{PLAN_ID}", headers=h),
            lambda h: self.client.get("/v1/me/nutrition-plans", headers=h),
            lambda h: self.client.get("/v1/food-catalog", headers=h),
        )
        for index, call in enumerate(calls):
            for headers in ({}, {"Authorization": "Bearer x"}, forged):
                with self.subTest(operation=index, headers=list(headers)):
                    response = call(headers)
                    self.assertEqual(response.status_code, 401)
                    self.assertEqual(response.json()["code"], "invalid_access_token")
                    self.assertEqual(response.headers["WWW-Authenticate"], "Bearer")
        self.assertEqual(self.upstreams.training_requests, [])


if __name__ == "__main__":
    unittest.main()
