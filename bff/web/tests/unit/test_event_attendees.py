"""BFF web, HU022 (ADR-039): filtro de borde `organizer` con `events.attendees.view`, reenvío
de la consulta a Eventos, composición de `display_name` con el directorio de Usuarios y resultado
parcial si Usuarios no responde."""

import json
import unittest
import uuid
from typing import Any

import httpx
from fastapi.testclient import TestClient
from test_training import Upstreams, token

from bff_web.main import create_app
from bff_web.settings import BffWebSettings

EVENT_ID = str(uuid.uuid4())
PATH = f"/v1/me/events/{EVENT_ID}/registrations"
ANA, LUIS = str(uuid.uuid4()), str(uuid.uuid4())


def item(athlete_id: str, minute: int) -> dict[str, Any]:
    return {
        "registration_id": str(uuid.uuid4()),
        "athlete_id": athlete_id,
        "status": "REGISTERED",
        "registered_at": f"2026-10-20T12:0{minute}:00Z",
        "applied_fee": None,
    }


PAGE: dict[str, Any] = {
    "items": [item(ANA, 1), item(LUIS, 2), item(ANA, 3)],
    "total": 3,
    "by_status": {"REGISTERED": 3},
    "next_cursor": "c2lndWllbnRl",
}


def error(code: str, details: list[dict[str, str]] | None = None) -> dict[str, Any]:
    return {"code": code, "message": "m", "correlation_id": None, "details": details or []}


class EventAttendeesRoutesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.upstreams = Upstreams()
        self.events_requests: list[httpx.Request] = []
        self.directory_requests: list[httpx.Request] = []
        self.events_response = httpx.Response(200, json=PAGE)
        self.directory_response: httpx.Response | type[Exception] = httpx.Response(
            200,
            json={
                "items": [
                    {"user_id": ANA, "full_name": "Ana Pérez"},
                    {"user_id": LUIS, "full_name": "Luis Gómez"},
                ]
            },
        )

        def events(request: httpx.Request) -> httpx.Response:
            self.events_requests.append(request)
            return self.events_response

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
                events_http=httpx.Client(
                    transport=httpx.MockTransport(events), base_url="http://events"
                ),
            )
        )

    def get(self, actor: str | None = "organizer", **params: str) -> Any:
        headers = {"Authorization": f"Bearer {token(actor)}"} if actor != "" else {}
        return self.client.get(PATH, params=params, headers=headers)

    def test_names_are_composed_from_the_directory(self) -> None:
        """CA1: `display_name` desde Usuarios; conteos, cursor y orden del servicio intactos."""
        response = self.get(status="ALL", limit="2", cursor="abc")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        body = response.json()
        self.assertEqual(
            [entry["display_name"] for entry in body["items"]],
            ["Ana Pérez", "Luis Gómez", "Ana Pérez"],
        )
        self.assertEqual(
            (body["total"], body["by_status"], body["next_cursor"]),
            (3, {"REGISTERED": 3}, "c2lndWllbnRl"),
        )
        self.assertEqual(body["sources"], [{"source": "users", "status": "OK"}])
        [sent] = self.events_requests
        self.assertEqual(sent.url.path, PATH)
        self.assertEqual(dict(sent.url.params), {"status": "ALL", "limit": "2", "cursor": "abc"})
        [lookup] = self.directory_requests
        self.assertEqual(json.loads(lookup.content), {"user_ids": [ANA, LUIS]}, "sin repetidos")
        self.assertNotIn("Authorization", lookup.headers, "contrato interno sin token")

    def test_empty_page_does_not_call_the_directory(self) -> None:
        """CA3: lista vacía y total cero, sin consultar Usuarios."""
        self.events_response = httpx.Response(
            200, json={"items": [], "total": 0, "by_status": {"REGISTERED": 0}, "next_cursor": None}
        )

        body = self.get().json()

        self.assertEqual((body["items"], body["total"]), ([], 0))
        self.assertEqual(body["sources"], [{"source": "users", "status": "OK"}])
        self.assertEqual(self.directory_requests, [])

    def test_users_down_gives_null_names_and_unavailable_source(self) -> None:
        """CA6: timeout, red caída o respuesta inesperada de Usuarios → nombres nulos."""
        for failure in (
            httpx.ReadTimeout,
            httpx.ConnectError,
            httpx.Response(500, json=error("boom")),
            httpx.Response(200, json={"sin": "items"}),
        ):
            with self.subTest(failure=repr(failure)):
                self.directory_response = failure
                response = self.get()
                self.assertEqual(response.status_code, 200)
                body = response.json()
                self.assertEqual({entry["display_name"] for entry in body["items"]}, {None})
                self.assertEqual(body["sources"], [{"source": "users", "status": "UNAVAILABLE"}])

    def test_edge_filter_only_organizers(self) -> None:
        for actor in ("athlete", "service_provider", None):
            with self.subTest(actor=actor):
                response = self.get(actor)
                self.assertEqual(
                    (response.status_code, response.json()["code"]), (403, "permission_required")
                )
                self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(self.get("").status_code, 401)
        self.assertEqual(self.events_requests, [])

    def test_events_errors_pass_through_unchanged(self) -> None:
        """CA2 y CA4: `404 event_not_found` y `422` del servicio sin alterar ni pedir nombres."""
        for status, payload in (
            (404, error("event_not_found")),
            (
                422,
                error("validation_error", [{"field": "query.cursor", "issue": "invalid_cursor"}]),
            ),
            (403, error("permission_required")),
        ):
            with self.subTest(status=status):
                self.events_response = httpx.Response(status, json=payload)
                response = self.get()
                self.assertEqual((response.status_code, response.json()), (status, payload))
        self.assertEqual(self.directory_requests, [])

    def test_unexpected_events_answers_are_503(self) -> None:
        for answer in (
            httpx.Response(500, json=error("boom")),
            httpx.Response(200, content=b"no-json"),
        ):
            with self.subTest(status=answer.status_code):
                self.events_response = answer
                response = self.get()
                self.assertEqual(
                    (response.status_code, response.json()["code"]), (503, "upstream_unavailable")
                )

    def test_logs_never_contain_names(self) -> None:
        with self.assertLogs(level="DEBUG") as logs:
            self.get()
            self.directory_response = httpx.ConnectError
            self.get()

        rendered = " ".join(logs.output) + " ".join(
            str(getattr(r, "fields", "")) for r in logs.records
        )
        for value in ("Ana Pérez", "Luis Gómez"):
            self.assertNotIn(value, rendered)


if __name__ == "__main__":
    unittest.main()
