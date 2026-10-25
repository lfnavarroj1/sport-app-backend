"""BFF web, HU040 (ADR-046 §4): citas de los servicios del profesional. Filtro de borde
`service_provider`, reenvío de los parámetros presentes, `athlete_display_name` desde el
directorio de Usuarios y resultado parcial si Usuarios no responde (CA6)."""

import json
import unittest
import uuid
from typing import Any

import httpx
from fastapi.testclient import TestClient
from test_training import Upstreams, token

from bff_web.main import create_app
from bff_web.settings import BffWebSettings

PATH = "/v1/me/appointments"
ANA, LUIS = str(uuid.uuid4()), str(uuid.uuid4())


def item(athlete_id: str, hour: int, status: str = "BOOKED") -> dict[str, Any]:
    return {
        "appointment_id": str(uuid.uuid4()),
        "athlete_id": athlete_id,
        "provider_id": str(uuid.uuid4()),
        "service_id": str(uuid.uuid4()),
        "availability_id": str(uuid.uuid4()),
        "service": {
            "name": "Entrenamiento virtual",
            "category": "COACHING",
            "modality": "VIRTUAL",
            "fee": {"amount": "80000.00", "currency": "COP", "billing_unit": "SESSION"},
        },
        "starts_at": f"2026-11-09T{hour:02d}:00:00Z",
        "ends_at": f"2026-11-09T{hour + 1:02d}:00:00Z",
        "time_zone": "America/Bogota",
        "status": status,
        "meeting_url": None,
        "booked_at": "2026-11-01T12:00:00Z",
        "cancelled_at": None if status == "BOOKED" else "2026-11-02T12:00:00Z",
        "version": 1 if status == "BOOKED" else 2,
    }


PAGE: dict[str, Any] = {
    "items": [item(ANA, 14), item(LUIS, 15), item(ANA, 16, "CANCELLED_BY_ATHLETE")],
    "next_cursor": "c2lndWllbnRl",
}


def error(code: str, details: list[dict[str, str]] | None = None) -> dict[str, Any]:
    return {"code": code, "message": "m", "correlation_id": None, "details": details or []}


class ProviderAppointmentsRoutesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.upstreams = Upstreams()
        self.services_requests: list[httpx.Request] = []
        self.directory_requests: list[httpx.Request] = []
        self.services_response: httpx.Response | type[Exception] = httpx.Response(200, json=PAGE)
        self.directory_response: httpx.Response | type[Exception] = httpx.Response(
            200,
            json={
                "items": [
                    {"user_id": ANA, "full_name": "Ana Pérez"},
                    {"user_id": LUIS, "full_name": "Luis Gómez"},
                ]
            },
        )

        def services(request: httpx.Request) -> httpx.Response:
            self.services_requests.append(request)
            if isinstance(self.services_response, type):
                raise self.services_response("servicios")
            return self.services_response

        def users(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v1/users/directory-lookup":
                self.directory_requests.append(request)
                if isinstance(self.directory_response, type):
                    raise self.directory_response("usuarios")
                return self.directory_response
            return self.upstreams.users(request)

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
                    transport=httpx.MockTransport(users), base_url="http://users"
                ),
                services_http=httpx.Client(
                    transport=httpx.MockTransport(services), base_url="http://services"
                ),
            )
        )

    def get(self, actor: str | None = "service_provider", **params: str) -> Any:
        headers = {"Authorization": f"Bearer {token(actor)}"} if actor != "" else {}
        return self.client.get(PATH, params=params, headers=headers)

    def test_names_are_composed_from_the_directory(self) -> None:
        """CA6: el profesional ve sus citas (también canceladas) con el nombre del deportista;
        `meeting_url` sigue `null`; cursor y orden del servicio intactos."""
        response = self.get(
            **{"from": "2026-11-01T00:00:00Z", "to": "2026-12-01T00:00:00Z", "limit": "3"}
        )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        body = response.json()
        self.assertEqual(
            [(entry["athlete_display_name"], entry["status"]) for entry in body["items"]],
            [
                ("Ana Pérez", "BOOKED"),
                ("Luis Gómez", "BOOKED"),
                ("Ana Pérez", "CANCELLED_BY_ATHLETE"),
            ],
        )
        self.assertEqual(
            [entry["appointment_id"] for entry in body["items"]],
            [entry["appointment_id"] for entry in PAGE["items"]],
        )
        self.assertTrue(all(entry["meeting_url"] is None for entry in body["items"]))
        self.assertEqual(body["next_cursor"], "c2lndWllbnRl")
        self.assertEqual(body["sources"], [{"source": "users", "status": "OK"}])
        [sent] = self.services_requests
        self.assertEqual(sent.url.path, PATH)
        self.assertEqual(
            dict(sent.url.params),
            {"from": "2026-11-01T00:00:00Z", "to": "2026-12-01T00:00:00Z", "limit": "3"},
        )
        self.assertTrue(sent.headers["Authorization"].startswith("Bearer "))
        [lookup] = self.directory_requests
        self.assertEqual(json.loads(lookup.content), {"user_ids": [ANA, LUIS]}, "sin repetidos")

    def test_directory_down_returns_list_without_names(self) -> None:
        self.directory_response = httpx.ConnectError

        response = self.get()

        body = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(all(entry["athlete_display_name"] is None for entry in body["items"]))
        self.assertEqual(body["sources"], [{"source": "users", "status": "UNAVAILABLE"}])

    def test_empty_page_does_not_call_the_directory(self) -> None:
        self.services_response = httpx.Response(200, json={"items": [], "next_cursor": None})

        response = self.get()

        self.assertEqual(response.json()["items"], [])
        self.assertEqual(self.directory_requests, [])

    def test_only_the_provider(self) -> None:
        """La reserva es del canal móvil: en web solo consulta el profesional."""
        for actor in ("athlete", "organizer"):
            with self.subTest(actor=actor):
                response = self.get(actor)
                self.assertEqual(
                    (response.status_code, response.json()["code"]), (403, "permission_required")
                )
        anonymous = self.get("")
        self.assertEqual(anonymous.status_code, 401)
        self.assertEqual(self.services_requests, [])

    def test_service_errors_pass_through(self) -> None:
        cases = [
            (422, error("validation_error", [{"field": "query.to", "issue": "required"}])),
            (403, error("permission_required")),
        ]
        for status, body in cases:
            with self.subTest(status=status):
                self.services_response = httpx.Response(status, json=body)
                response = self.get(**{"from": "2026-11-01T00:00:00Z"})
                self.assertEqual(
                    (response.status_code, response.json()["code"]), (status, body["code"])
                )
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_service_down(self) -> None:
        for failure, status, code in (
            (httpx.ReadTimeout, 504, "upstream_timeout"),
            (httpx.ConnectError, 503, "upstream_unavailable"),
        ):
            with self.subTest(failure=failure):
                self.services_response = failure
                response = self.get()
                self.assertEqual((response.status_code, response.json()["code"]), (status, code))
        self.services_response = httpx.Response(200, content=b"no-json")
        broken = self.get()
        self.assertEqual(broken.status_code, 503)


if __name__ == "__main__":
    unittest.main()
