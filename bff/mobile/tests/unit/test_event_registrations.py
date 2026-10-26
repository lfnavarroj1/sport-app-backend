"""BFF móvil, HU041: detalle, inscripción y «mis inscripciones» se reenvían sin alterar."""

import unittest
import uuid
from typing import Any

import httpx
from fastapi.testclient import TestClient
from test_capabilities import FREE_CAPABILITIES, Upstream, token

from bff_mobile.main import create_app
from bff_mobile.settings import BffMobileSettings

EVENT_ID = str(uuid.uuid4())
REGISTRATION_ID = str(uuid.uuid4())
LOCATION = {"name": "Parque Simón Bolívar", "city": "Bogotá", "country_code": "CO"}
REGISTRATION = {
    "registration_id": REGISTRATION_ID,
    "event_id": EVENT_ID,
    "status": "REGISTERED",
    "registered_at": "2026-10-12T22:30:00Z",
    "applied_fee": None,
    "version": 1,
    "event": {
        "name": "Carrera 10K Bogotá",
        "starts_at": "2026-11-15T11:00:00Z",
        "ends_at": "2026-11-15T14:00:00Z",
        "time_zone": "America/Bogota",
        "location": LOCATION,
    },
}


class EventRegistrationRoutesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.upstream = Upstream(
            {"subscription_status": "ACTIVE", "capabilities": FREE_CAPABILITIES}
        )
        self.reply: httpx.Response = httpx.Response(200, json={})
        self.failure: type[Exception] | None = None
        self.requests: list[httpx.Request] = []

        def events(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            if self.failure is not None:
                raise self.failure("eventos")
            return self.reply

        settings = BffMobileSettings(
            app_env="test",  # type: ignore[arg-type]
            users_management_base_url="http://users.invalid",  # type: ignore[arg-type]
            affiliation_management_base_url="http://affiliation.invalid",  # type: ignore[arg-type]
            training_management_base_url="http://training.invalid",  # type: ignore[arg-type]
            events_management_base_url="http://events.invalid",  # type: ignore[arg-type]
            complementary_services_base_url="http://services.invalid",  # type: ignore[arg-type]
            third_party_management_base_url="http://third-party.invalid",  # type: ignore[arg-type]
        )
        self.client = TestClient(
            create_app(
                settings,
                users_http=httpx.Client(
                    base_url="http://users", transport=httpx.MockTransport(self.upstream.users)
                ),
                affiliation_http=httpx.Client(
                    base_url="http://affiliation",
                    transport=httpx.MockTransport(self.upstream.affiliation),
                ),
                training_http=httpx.Client(
                    base_url="http://training",
                    transport=httpx.MockTransport(lambda _: httpx.Response(500)),
                ),
                events_http=httpx.Client(
                    base_url="http://events", transport=httpx.MockTransport(events)
                ),
            )
        )

    def auth(self, actor: str = "athlete", **headers: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token(actor)}", **headers}

    def test_register_forwards_key_correlation_and_headers(self) -> None:
        """CA1 y CA6: la clave y la correlación llegan tal cual; `Location` y la marca de
        reintento vuelven al cliente."""
        key, correlation = str(uuid.uuid4()), str(uuid.uuid4())
        self.reply = httpx.Response(
            201,
            json=REGISTRATION,
            headers={
                "Location": f"/v1/me/event-registrations/{REGISTRATION_ID}",
                "Idempotent-Replayed": "true",
            },
        )

        response = self.client.post(
            f"/v1/events/{EVENT_ID}/registrations",
            headers=self.auth(**{"Idempotency-Key": key, "X-Correlation-ID": correlation}),
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json(), REGISTRATION)
        self.assertEqual(
            response.headers["Location"], f"/v1/me/event-registrations/{REGISTRATION_ID}"
        )
        self.assertEqual(response.headers["Idempotent-Replayed"], "true")
        sent = self.requests[0]
        self.assertEqual(
            (sent.method, sent.url.path), ("POST", f"/v1/events/{EVENT_ID}/registrations")
        )
        self.assertEqual(sent.headers["Idempotency-Key"], key)
        self.assertEqual(sent.headers["X-Correlation-ID"], correlation)
        self.assertEqual(sent.content, b"", "la inscripción no lleva cuerpo ni `athlete_id`")

    def test_service_errors_pass_through_unchanged(self) -> None:
        """CA2-CA6: los códigos y `details` del servicio llegan sin reinterpretar."""
        cases: list[tuple[int, dict[str, Any]]] = [
            (404, {"code": "event_not_found"}),
            (
                409,
                {
                    "code": "event_registration_exists",
                    "details": [{"field": "registration_id", "issue": REGISTRATION_ID}],
                },
            ),
            (409, {"code": "event_full"}),
            (409, {"code": "event_registration_closed"}),
            (409, {"code": "idempotency_key_reused"}),
            (
                422,
                {
                    "code": "validation_error",
                    "details": [{"field": "header.Idempotency-Key", "issue": "required"}],
                },
            ),
        ]
        for status, body in cases:
            self.reply = httpx.Response(
                status, json={"message": "m", "correlation_id": "c", **body}
            )
            with self.subTest(code=body["code"]):
                response = self.client.post(
                    f"/v1/events/{EVENT_ID}/registrations", headers=self.auth()
                )
                self.assertEqual(response.status_code, status)
                self.assertEqual(response.json()["code"], body["code"])
                self.assertEqual(response.json().get("details"), body.get("details"))
        self.assertNotIn("Idempotency-Key", self.requests[-1].headers, "sin clave no se inventa")

    def test_detail_and_my_registrations(self) -> None:
        """CA7: detalle con mi inscripción y listado propio con parámetros reenviados."""
        detail = {"event_id": EVENT_ID, "my_registration": {"registration_id": REGISTRATION_ID}}
        self.reply = httpx.Response(200, json=detail)
        got_detail = self.client.get(f"/v1/events/{EVENT_ID}", headers=self.auth())
        self.reply = httpx.Response(200, json={"items": [REGISTRATION], "next_cursor": None})
        listed = self.client.get(
            "/v1/me/event-registrations?status=REGISTERED&limit=5&cursor=abc",
            headers=self.auth(),
        )
        unfiltered = self.client.get("/v1/me/event-registrations", headers=self.auth())

        self.assertEqual((got_detail.status_code, got_detail.json()), (200, detail))
        self.assertEqual(listed.json()["items"], [REGISTRATION])
        self.assertEqual(self.requests[0].url.path, f"/v1/events/{EVENT_ID}")
        self.assertEqual(
            dict(self.requests[1].url.params),
            {"status": "REGISTERED", "limit": "5", "cursor": "abc"},
        )
        self.assertEqual(unfiltered.status_code, 200)
        self.assertEqual(dict(self.requests[2].url.params), {})

    def test_edge_rejections_never_reach_the_service(self) -> None:
        organizer = self.client.post(
            f"/v1/events/{EVENT_ID}/registrations", headers=self.auth("organizer")
        )
        anonymous = self.client.get("/v1/me/event-registrations")
        invalid = self.client.get(
            "/v1/me/event-registrations", headers={"Authorization": "Bearer roto"}
        )
        bad_id = self.client.get("/v1/events/no-es-uuid", headers=self.auth())

        self.assertEqual(
            (organizer.status_code, organizer.json()["code"]), (403, "permission_required")
        )
        self.assertEqual(anonymous.status_code, 401)
        self.assertEqual(invalid.json()["code"], "invalid_access_token")
        self.assertEqual(bad_id.status_code, 422)
        self.assertEqual(self.requests, [])

    def test_upstream_failures(self) -> None:
        self.failure = httpx.ConnectTimeout
        timeout = self.client.post(f"/v1/events/{EVENT_ID}/registrations", headers=self.auth())
        self.failure = httpx.ConnectError
        down = self.client.get(f"/v1/events/{EVENT_ID}", headers=self.auth())
        self.failure = None
        self.reply = httpx.Response(500, json={"code": "internal_error"})
        unexpected = self.client.get("/v1/me/event-registrations", headers=self.auth())

        self.assertEqual((timeout.status_code, timeout.json()["code"]), (504, "upstream_timeout"))
        self.assertEqual((down.status_code, down.json()["code"]), (503, "upstream_unavailable"))
        self.assertEqual(unexpected.status_code, 503)


if __name__ == "__main__":
    unittest.main()
