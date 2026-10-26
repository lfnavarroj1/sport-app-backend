"""HU014: cliente del contrato interno `GET /v1/users/{user_id}/nutrition-restrictions`.

Usa un transporte simulado (sin red). Cualquier respuesta distinta de un `200` válido es
indisponibilidad; los valores de la respuesta nunca aparecen en los logs.
"""

import unittest
import uuid
from collections.abc import Callable
from typing import Any

import httpx

from sportapp_platform.correlation import correlation_id_var
from training_management.application.errors import NutritionRestrictionsUnavailable
from training_management.infrastructure.integrations.users import UsersNutritionRestrictions

Handler = Callable[[httpx.Request], httpx.Response]


class UsersNutritionRestrictionsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        # Un solo cliente: crear cada `httpx.Client` es costoso en Windows (carga `certifi`).
        cls.handlers: list[Handler] = []

        def dispatch(request: httpx.Request) -> httpx.Response:
            return cls.handlers[-1](request)

        cls.http = httpx.Client(
            transport=httpx.MockTransport(dispatch),
            base_url="http://users",
        )
        cls.client = UsersNutritionRestrictions(cls.http)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.http.close()

    def respond(self, handler: Handler) -> None:
        self.handlers.append(handler)

    def test_reads_restrictions_and_propagates_correlation(self) -> None:
        athlete_id, correlation = uuid.uuid4(), str(uuid.uuid4())
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(
                200,
                json={
                    "restrictions": ["GLUTEN_FREE"],
                    "allergies": ["PEANUTS"],
                    "diet_pattern": "VEGAN",
                    "version": 4,
                },
            )

        self.respond(handler)
        token = correlation_id_var.set(correlation)
        try:
            result = self.client.get(athlete_id)
        finally:
            correlation_id_var.reset(token)

        self.assertEqual(
            (result.restrictions, result.allergies, result.diet_pattern, result.version),
            (("GLUTEN_FREE",), ("PEANUTS",), "VEGAN", 4),
        )
        self.assertFalse(result.habits_missing)
        self.assertEqual(requests[0].url.path, f"/v1/users/{athlete_id}/nutrition-restrictions")
        self.assertEqual(requests[0].headers["X-Correlation-ID"], correlation)
        self.assertNotIn("Authorization", requests[0].headers)

    def test_without_habits(self) -> None:
        self.respond(
            lambda _: httpx.Response(
                200,
                json={"restrictions": [], "allergies": [], "diet_pattern": None, "version": None},
            )
        )

        result = self.client.get(uuid.uuid4())

        self.assertTrue(result.habits_missing)

    def test_any_other_answer_is_unavailable_and_logs_no_values(self) -> None:
        def raise_timeout(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("lento", request=request)

        def raise_transport(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("caído", request=request)

        valid: dict[str, Any] = {
            "restrictions": ["KETO"],
            "allergies": ["SESAME"],
            "diet_pattern": "VEGAN",
            "version": 1,
        }
        cases: dict[str, Handler] = {
            "timeout": raise_timeout,
            "transport_error": raise_transport,
            "status_404": lambda _: httpx.Response(404, json={"code": "user_not_found"}),
            "status_500": lambda _: httpx.Response(500),
            "invalid_json": lambda _: httpx.Response(200, content=b"no-json"),
            "not_object": lambda _: httpx.Response(200, json=["KETO"]),
            "bad_list": lambda _: httpx.Response(200, json={**valid, "allergies": "SESAME"}),
            "bad_item": lambda _: httpx.Response(200, json={**valid, "restrictions": [1]}),
            "bad_diet": lambda _: httpx.Response(200, json={**valid, "diet_pattern": 3}),
            "bad_version": lambda _: httpx.Response(200, json={**valid, "version": "1"}),
            "bool_version": lambda _: httpx.Response(200, json={**valid, "version": True}),
        }
        for name, handler in cases.items():
            self.respond(handler)
            with (
                self.subTest(case=name),
                self.assertLogs("sportapp.training", level="WARNING") as logs,
                self.assertRaises(NutritionRestrictionsUnavailable) as error,
            ):
                self.client.get(uuid.uuid4())
            self.assertEqual(error.exception.code, "upstream_unavailable")
            output = "\n".join(logs.output)
            for value in ("KETO", "SESAME", "VEGAN"):
                self.assertNotIn(value, output)


if __name__ == "__main__":
    unittest.main()
