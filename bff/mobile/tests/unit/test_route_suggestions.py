"""BFF móvil, HU026 (ADR-041): rutas sugeridas.

Borde (`athlete` + `training.routes.suggest`), contexto interno de Usuarios con timeout de
300 ms y caché de 5 minutos, filtros explícitos hacia Entrenamientos, `author_name` por
`directory-lookup` y degradación a lista no personalizada cuando Usuarios no responde.
Usuarios, Afiliación y Entrenamientos se simulan con `httpx.MockTransport`.
"""

import json
import unittest
import uuid
from typing import Any

import httpx
from fastapi.testclient import TestClient
from test_capabilities import FREE_CAPABILITIES, SIGNER, Upstream, token

from bff_mobile.api.route_suggestions import AthleteContext, AthleteContextCache
from bff_mobile.main import create_app
from bff_mobile.settings import BffMobileSettings
from sportapp_platform.tokens import public_jwk

SUGGESTIONS = "/v1/me/suggestions/routes"
ORGANIZER_ID = str(uuid.uuid4())
CONTEXT = {
    "sports": ["running", "cycling"],
    "primary_goal": "PERFORMANCE",
    "city": "Medellín",
    "country_code": "CO",
    "birth_date": "1990-05-14",
}


def route(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "route_id": str(uuid.uuid4()),
        "organizer_id": ORGANIZER_ID,
        "name": "Vuelta al embalse",
        "description": "Recorrido llano.",
        "sport": "running",
        "distance_km": "12.5",
        "difficulty": "MODERATE",
        "city": "Medellín",
        "country_code": "CO",
        "start_point": {"lat": 6.2442, "lng": -75.5812},
        "waypoints": [{"lat": 6.25, "lng": -75.57}],
        "source": "ORGANIZER",
        "status": "PUBLISHED",
        "version": 1,
        "created_at": "2026-10-24T12:00:00Z",
        "updated_at": "2026-10-24T12:00:00Z",
        "published_at": "2026-10-24T12:00:00Z",
        "retired_at": None,
        "reasons": ["SPORT_MATCH", "CITY_MATCH"],
    }
    values.update(overrides)
    return values


def error(code: str) -> dict[str, Any]:
    return {"code": code, "message": "m", "correlation_id": None, "details": []}


class Users:
    """JWKS, contexto del deportista y directorio de nombres."""

    def __init__(self) -> None:
        self.context: httpx.Response | type[Exception] = httpx.Response(200, json=CONTEXT)
        self.directory: httpx.Response | type[Exception] = httpx.Response(
            200, json={"items": [{"user_id": ORGANIZER_ID, "full_name": "Club Andino"}]}
        )
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/.well-known/jwks.json":
            return httpx.Response(200, json={"keys": [public_jwk(SIGNER.public_key())]})
        self.requests.append(request)
        reply = self.context if request.url.path.endswith("/athlete-context") else self.directory
        if isinstance(reply, type):
            raise reply("usuarios")
        return reply

    def paths(self) -> list[str]:
        return [request.url.path for request in self.requests]


class RouteSuggestionsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.affiliation = Upstream(
            {"subscription_status": "ACTIVE", "capabilities": FREE_CAPABILITIES}
        )
        self.users = Users()
        self.reply: httpx.Response | type[Exception] = httpx.Response(
            200, json={"items": [route()], "personalized": True}
        )
        self.training_requests: list[httpx.Request] = []

        def training(request: httpx.Request) -> httpx.Response:
            self.training_requests.append(request)
            if isinstance(self.reply, type):
                raise self.reply("entrenamientos")
            return self.reply

        self.client = TestClient(
            create_app(
                BffMobileSettings(
                    app_env="test",  # type: ignore[arg-type]
                    users_management_base_url="http://users.invalid",  # type: ignore[arg-type]
                    affiliation_management_base_url="http://affiliation.invalid",  # type: ignore[arg-type]
                    training_management_base_url="http://training.invalid",  # type: ignore[arg-type]
                    events_management_base_url="http://events.invalid",  # type: ignore[arg-type]
                    complementary_services_base_url="http://services.invalid",  # type: ignore[arg-type]
                ),
                users_http=httpx.Client(
                    base_url="http://users", transport=httpx.MockTransport(self.users)
                ),
                affiliation_http=httpx.Client(
                    base_url="http://affiliation",
                    transport=httpx.MockTransport(self.affiliation.affiliation),
                ),
                training_http=httpx.Client(
                    base_url="http://training", transport=httpx.MockTransport(training)
                ),
                events_http=httpx.Client(
                    base_url="http://events",
                    transport=httpx.MockTransport(lambda _: httpx.Response(500)),
                ),
            )
        )
        self.athlete_id = uuid.uuid4()

    def get(self, actor: str | None = "athlete", **params: str) -> httpx.Response:
        headers = {}
        if actor is not None:
            headers["Authorization"] = f"Bearer {token(actor, sub=str(self.athlete_id))}"
        return self.client.get(SUGGESTIONS, params=params, headers=headers)

    def sent_params(self) -> list[tuple[str, str]]:
        return list(self.training_requests[-1].url.params.multi_items())

    def test_personalized_suggestions_carry_reasons_author_and_sources(self) -> None:
        """CA3: deportes, ciudad y país del contexto como filtros; motivos y autor."""
        response = self.get(limit="10")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        body = response.json()
        self.assertEqual(
            self.sent_params(),
            [
                ("sport", "running"),
                ("sport", "cycling"),
                ("city", "Medellín"),
                ("country_code", "CO"),
                ("limit", "10"),
            ],
        )
        [item] = body["items"]
        self.assertEqual(item["reference"], {"type": "route", "id": item["route_id"]})
        self.assertEqual(
            (item["title"], item["summary"], item["author_name"], item["reasons"]),
            ("Vuelta al embalse", "Recorrido llano.", "Club Andino", ["SPORT_MATCH", "CITY_MATCH"]),
        )
        self.assertEqual(item["start_point"], {"lat": 6.2442, "lng": -75.5812})
        self.assertNotIn("organizer_id", item)
        self.assertEqual((body["personalized"], body["missing_context"]), (True, []))
        self.assertEqual(body["sources"], [{"source": "users", "status": "OK"}])
        self.assertTrue(body["generated_at"].endswith("Z"))
        context_request, lookup = self.users.requests
        self.assertEqual(context_request.url.path, f"/v1/users/{self.athlete_id}/athlete-context")
        self.assertNotIn("Authorization", context_request.headers)
        self.assertEqual(context_request.extensions["timeout"]["read"], 0.3)
        self.assertEqual(json.loads(lookup.content), {"user_ids": [ORGANIZER_ID]})

    def test_without_sports_the_list_is_not_personalized(self) -> None:
        """CA3: sin deportes → `personalized: false` y `missing_context: ["sports"]`."""
        self.users.context = httpx.Response(200, json={**CONTEXT, "sports": []})
        self.reply = httpx.Response(200, json={"items": [route(reasons=[])], "personalized": False})

        body = self.get().json()

        self.assertEqual(self.sent_params(), [("city", "Medellín"), ("country_code", "CO")])
        self.assertEqual((body["personalized"], body["missing_context"]), (False, ["sports"]))
        self.assertEqual(body["items"][0]["reasons"], [])

    def test_users_down_or_slow_gives_a_non_personalized_list(self) -> None:
        """HA011: si solo falla Usuarios, la lista llega sin personalizar y se informa."""
        for failure in (httpx.ConnectError, httpx.ReadTimeout):
            with self.subTest(failure=failure.__name__):
                self.users.context = failure
                self.users.directory = failure
                self.reply = httpx.Response(
                    200, json={"items": [route(reasons=[])], "personalized": False}
                )

                response = self.get()

                body = response.json()
                self.assertEqual(response.status_code, 200)
                self.assertEqual(self.sent_params(), [])
                self.assertEqual(
                    (body["personalized"], body["missing_context"]), (False, ["sports", "city"])
                )
                self.assertEqual(body["sources"], [{"source": "users", "status": "UNAVAILABLE"}])
                self.assertIsNone(body["items"][0]["author_name"])

    def test_unknown_account_or_invalid_context_is_treated_as_missing(self) -> None:
        self.users.context = httpx.Response(404, json=error("user_not_found"))
        self.reply = httpx.Response(200, json={"items": [], "personalized": False})

        body = self.get().json()

        self.assertEqual(body["missing_context"], ["sports", "city"])
        self.assertEqual(body["sources"], [{"source": "users", "status": "OK"}])
        self.assertNotIn("/v1/users/directory-lookup", self.users.paths())
        self.users.context = httpx.Response(200, json={"sports": "running"})
        self.assertEqual(
            self.get().json()["sources"], [{"source": "users", "status": "UNAVAILABLE"}]
        )

    def test_context_is_cached_per_athlete(self) -> None:
        self.get()
        self.get()

        context_calls = [path for path in self.users.paths() if path.endswith("athlete-context")]
        self.assertEqual(len(context_calls), 1)

    def test_directory_failure_keeps_the_items_without_author(self) -> None:
        self.users.directory = httpx.Response(500, json=error("x"))

        body = self.get().json()

        self.assertEqual(body["personalized"], True)
        self.assertIsNone(body["items"][0]["author_name"])
        self.assertEqual(body["sources"], [{"source": "users", "status": "UNAVAILABLE"}])

    def test_edge_rejects_other_actors_missing_token_and_capability(self) -> None:
        organizer = self.get(actor="organizer")
        missing = self.get(actor=None)
        self.affiliation.snapshot = {"subscription_status": "ACTIVE", "capabilities": []}
        without = self.get()
        self.affiliation.status = 0
        down = self.get()

        self.assertEqual(
            (organizer.status_code, organizer.json()["code"]), (403, "permission_required")
        )
        self.assertEqual(missing.status_code, 401)
        self.assertEqual(
            (without.status_code, without.json()["code"]), (403, "capability_required")
        )
        self.assertEqual(down.status_code, 503)
        self.assertEqual(without.headers["Cache-Control"], "no-store")
        self.assertEqual((self.training_requests, self.users.requests), ([], []))

    def test_training_failures(self) -> None:
        """HA011: si falla el dominio fuente, `503`/`504`; sus `422` y `403` sin alterar."""
        cases = (
            (httpx.Response(422, json=error("validation_error")), 422),
            (httpx.Response(403, json=error("capability_required")), 403),
            (httpx.Response(503, json=error("upstream_unavailable")), 503),
            (httpx.Response(500, json=error("x")), 503),
            (httpx.Response(200, json={"items": [{"route_id": "x"}]}), 503),
            (httpx.ReadTimeout, 504),
            (httpx.ConnectError, 503),
        )
        for reply, status in cases:
            with self.subTest(reply=reply, status=status):
                self.reply = reply
                self.assertEqual(self.get().status_code, status)


class AthleteContextCacheTest(unittest.TestCase):
    def test_entries_expire_and_the_cache_is_bounded(self) -> None:
        now = [0.0]
        cache = AthleteContextCache(ttl_seconds=300, max_entries=2, clock=lambda: now[0])
        first, second, third = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        context = AthleteContext(sports=["running"], city=None, country_code=None)

        cache.put(first, context)
        cache.put(second, context)
        cache.put(third, context)
        now[0] = 299.0
        self.assertIsNone(cache.get(first))
        self.assertEqual(cache.get(second), context)
        now[0] = 300.0
        self.assertIsNone(cache.get(second))


if __name__ == "__main__":
    unittest.main()
