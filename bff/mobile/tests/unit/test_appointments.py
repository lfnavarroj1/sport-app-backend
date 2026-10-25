"""BFF móvil, HU040: reservar, cancelar y listar sesiones virtuales (ADR-046).

El borde exige `athlete`; reservar exige además `services.virtual_sessions.schedule` vigente
en Afiliación (sin respuesta, también por timeout, `503`). Cancelar y listar no consultan la
capacidad. Cuerpo, clave, token, correlación, parámetros, estados y cabeceras del servicio
llegan sin alterar. El router se monta solo, con los mismos clientes que `create_app`.
"""

import json
import unittest
import uuid
from typing import Any

import httpx
from fastapi.testclient import TestClient
from test_capabilities import token
from test_devices import SlowableUpstream, error

from bff_mobile.api.appointments import build_appointments_router
from bff_mobile.clients.affiliation_management import AffiliationManagementClient
from bff_mobile.clients.complementary_services import ComplementaryServicesClient
from bff_mobile.clients.users_management import UsersManagementClient
from bff_mobile.settings import BffMobileSettings
from sportapp_platform.app import create_service_app
from sportapp_platform.tokens import CachedJwksVerifier

CAPABILITY = "services.virtual_sessions.schedule"
PATH = "/v1/me/appointments"
APPOINTMENT_ID = uuid.uuid4()
BODY = {"service_id": str(uuid.uuid4()), "availability_id": str(uuid.uuid4())}
APPOINTMENT: dict[str, Any] = {
    "appointment_id": str(APPOINTMENT_ID),
    "athlete_id": str(uuid.uuid4()),
    "provider_id": str(uuid.uuid4()),
    "service_id": BODY["service_id"],
    "availability_id": BODY["availability_id"],
    "service": {
        "name": "Entrenamiento virtual",
        "category": "COACHING",
        "modality": "VIRTUAL",
        "fee": {"amount": "80000.00", "currency": "COP", "billing_unit": "SESSION"},
    },
    "starts_at": "2026-11-02T13:00:00Z",
    "ends_at": "2026-11-02T14:00:00Z",
    "time_zone": "America/Bogota",
    "status": "BOOKED",
    "meeting_url": None,
    "booked_at": "2026-10-25T12:00:00Z",
    "cancelled_at": None,
    "version": 1,
}
CANCELLED = {
    **APPOINTMENT,
    "status": "CANCELLED_BY_ATHLETE",
    "cancelled_at": "2026-10-26T09:00:00Z",
    "version": 2,
}


class AppointmentsRoutesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.upstream = SlowableUpstream(
            {"subscription_status": "ACTIVE", "capabilities": [CAPABILITY]}
        )
        self.reply: httpx.Response = httpx.Response(200, json={})
        self.failure: type[Exception] | None = None
        self.requests: list[httpx.Request] = []

        def services(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            if self.failure is not None:
                raise self.failure("servicios")
            return self.reply

        settings = BffMobileSettings(
            app_env="test",  # type: ignore[arg-type]
            users_management_base_url="http://users.invalid",  # type: ignore[arg-type]
            affiliation_management_base_url="http://affiliation.invalid",  # type: ignore[arg-type]
            training_management_base_url="http://training.invalid",  # type: ignore[arg-type]
            events_management_base_url="http://events.invalid",  # type: ignore[arg-type]
            complementary_services_base_url="http://services.invalid",  # type: ignore[arg-type]
        )
        users = UsersManagementClient(
            httpx.Client(
                base_url="http://users", transport=httpx.MockTransport(self.upstream.users)
            )
        )
        affiliation = AffiliationManagementClient(
            httpx.Client(
                base_url="http://affiliation",
                transport=httpx.MockTransport(self.upstream.affiliation),
            )
        )
        verifier = CachedJwksVerifier(
            users.jwks, issuer=settings.jwt_issuer, audience=settings.jwt_audience
        )
        app = create_service_app(
            service_name="bff-mobile",
            title="SportApp - BFF móvil",
            api_version="test",
            settings=settings,
            routers=[
                build_appointments_router(
                    verify_token=verifier.verify,
                    athlete_snapshot=affiliation.user_capabilities,
                    services=ComplementaryServicesClient(
                        httpx.Client(
                            base_url="http://services", transport=httpx.MockTransport(services)
                        )
                    ),
                )
            ],
        )
        self.client = TestClient(app)

    def auth(self, actor: str = "athlete", **headers: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token(actor)}", **headers}

    def book(self, headers: dict[str, str] | None = None, **kwargs: Any) -> httpx.Response:
        return self.client.post(PATH, json=kwargs.pop("json", BODY), headers=headers, **kwargs)

    def every_operation(self, headers: dict[str, str] | None = None) -> dict[str, Any]:
        return {
            "book": self.book(headers),
            "cancel": self.client.delete(f"{PATH}/{APPOINTMENT_ID}", headers=headers),
            "list": self.client.get(PATH, headers=headers),
        }

    # Reservar

    def test_book_forwards_body_key_token_and_correlation(self) -> None:
        key, correlation = str(uuid.uuid4()), str(uuid.uuid4())
        self.reply = httpx.Response(
            201,
            json=APPOINTMENT,
            headers={"Location": f"{PATH}/{APPOINTMENT_ID}", "Idempotent-Replayed": "true"},
        )
        user_token = token("athlete")
        body = {**BODY, "extra": True}

        response = self.book(
            {
                "Authorization": f"Bearer {user_token}",
                "Idempotency-Key": key,
                "X-Correlation-ID": correlation,
            },
            json=body,
        )

        self.assertEqual((response.status_code, response.json()), (201, APPOINTMENT))
        self.assertEqual(response.headers["Location"], f"{PATH}/{APPOINTMENT_ID}")
        self.assertEqual(response.headers["Idempotent-Replayed"], "true")
        sent = self.requests[0]
        self.assertEqual((sent.method, sent.url.path), ("POST", PATH))
        self.assertEqual(json.loads(sent.content), body)
        self.assertEqual(sent.headers["Idempotency-Key"], key)
        self.assertEqual(sent.headers["Authorization"], f"Bearer {user_token}")
        self.assertEqual(sent.headers["X-Correlation-ID"], correlation)
        self.assertEqual(self.upstream.affiliation_paths[0].rsplit("/", 2)[-1], "capabilities")

    def test_book_without_key_or_body_is_left_to_the_service(self) -> None:
        problem = error("validation_error", [{"field": "Idempotency-Key", "issue": "required"}])
        self.reply = httpx.Response(422, json=problem)

        response = self.client.post(PATH, headers=self.auth())

        self.assertEqual((response.status_code, response.json()), (422, problem))
        sent = self.requests[0]
        self.assertNotIn("Idempotency-Key", sent.headers)
        self.assertEqual(sent.content, b"")

    def test_book_service_errors_pass_through(self) -> None:
        """CA2/CA3: cupo, unicidad y elegibilidad llegan del servicio sin alterar."""
        existing = str(uuid.uuid4())
        cases = [
            (409, error("availability_full")),
            (409, error("appointment_exists", [{"field": "appointment_id", "issue": existing}])),
            (409, error("idempotency_key_reused")),
            (422, error("service_not_bookable")),
            (422, error("availability_not_bookable")),
            (
                422,
                error("validation_error", [{"field": "service_id", "issue": "unknown_value"}]),
            ),
            (401, error("invalid_access_token")),
            (403, error("capability_required")),
            (503, error("upstream_unavailable")),
        ]
        for status, body in cases:
            self.reply = httpx.Response(status, json=body)
            response = self.book(self.auth(**{"Idempotency-Key": str(uuid.uuid4())}))
            with self.subTest(code=body["code"]):
                self.assertEqual((response.status_code, response.json()), (status, body))

    def test_book_without_the_capability_is_403_without_calling_the_service(self) -> None:
        for snapshot in (
            {"subscription_status": "ACTIVE", "capabilities": []},
            {"subscription_status": "ACTIVE", "capabilities": ["services.support.suggest"]},
            {"subscription_status": "ENDED", "capabilities": [CAPABILITY]},
            {"subscription_status": "NONE"},
        ):
            self.upstream.snapshot = snapshot
            response = self.book(self.auth())
            with self.subTest(snapshot=snapshot):
                self.assertEqual(
                    (response.status_code, response.json()["code"]), (403, "capability_required")
                )
        self.assertEqual(self.requests, [])

    def test_book_with_affiliation_down_slow_or_failing_is_503(self) -> None:
        """La capacidad no evaluable nunca se concede: `503`, también por timeout."""
        self.upstream.status = 0
        down = self.book(self.auth())
        self.upstream.status = 500
        failing = self.book(self.auth())
        self.upstream.status, self.upstream.slow = 200, True
        slow = self.book(self.auth())
        for name, response in {"down": down, "failing": failing, "slow": slow}.items():
            with self.subTest(case=name):
                self.assertEqual(
                    (response.status_code, response.json()["code"]), (503, "upstream_unavailable")
                )
        self.assertEqual(self.requests, [])

    # Cancelar y listar no consultan la capacidad

    def test_cancel_relays_the_cancelled_appointment_without_capability(self) -> None:
        self.upstream.snapshot = {"subscription_status": "NONE", "capabilities": []}
        self.reply = httpx.Response(200, json=CANCELLED)

        response = self.client.delete(f"{PATH}/{APPOINTMENT_ID}", headers=self.auth())

        self.assertEqual((response.status_code, response.json()), (200, CANCELLED))
        sent = self.requests[0]
        self.assertEqual((sent.method, sent.url.path), ("DELETE", f"{PATH}/{APPOINTMENT_ID}"))
        self.assertEqual(self.upstream.affiliation_paths, [])

    def test_cancel_service_errors_pass_through(self) -> None:
        """CA4: la ventana de 24 h y la propiedad las decide el servicio."""
        cases = [
            (404, error("appointment_not_found")),
            (422, error("cancellation_window_closed")),
            (403, error("permission_required")),
            (401, error("invalid_access_token")),
            (503, error("upstream_unavailable")),
        ]
        for status, body in cases:
            self.reply = httpx.Response(status, json=body)
            response = self.client.delete(f"{PATH}/{APPOINTMENT_ID}", headers=self.auth())
            with self.subTest(code=body["code"]):
                self.assertEqual((response.status_code, response.json()), (status, body))

    def test_cancel_with_a_malformed_identifier_is_422_without_calling_the_service(self) -> None:
        response = self.client.delete(f"{PATH}/no-es-uuid", headers=self.auth())

        self.assertEqual((response.status_code, response.json()["code"]), (422, "validation_error"))
        self.assertEqual(self.requests, [])

    def test_list_forwards_only_present_parameters_as_they_arrive(self) -> None:
        self.upstream.snapshot = {"subscription_status": "NONE", "capabilities": []}
        page = {"items": [APPOINTMENT, CANCELLED], "next_cursor": "c2"}
        self.reply = httpx.Response(200, json=page)
        query = {
            "from": "2026-11-01T00:00:00-05:00",
            "to": "2026-11-30T00:00:00Z",
            "status": "BOOKED",
            "limit": "x",
            "cursor": "c1",
        }

        response = self.client.get(PATH, params=query, headers=self.auth())

        self.assertEqual((response.status_code, response.json()), (200, page))
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        sent = self.requests[0]
        self.assertEqual((sent.method, sent.url.path), ("GET", PATH))
        self.assertEqual(dict(sent.url.params), query)
        self.assertEqual(self.upstream.affiliation_paths, [])

        self.client.get(PATH, params={"status": "CANCELLED_BY_ATHLETE"}, headers=self.auth())
        self.assertEqual(dict(self.requests[1].url.params), {"status": "CANCELLED_BY_ATHLETE"})

    def test_list_service_errors_pass_through_without_cache(self) -> None:
        cases = [
            (422, error("validation_error", [{"field": "query.to", "issue": "required"}])),
            (403, error("permission_required")),
            (401, error("invalid_access_token")),
            (503, error("upstream_unavailable")),
        ]
        for status, body in cases:
            self.reply = httpx.Response(status, json=body)
            response = self.client.get(PATH, params={"from": "x"}, headers=self.auth())
            with self.subTest(code=body["code"]):
                self.assertEqual((response.status_code, response.json()), (status, body))
                self.assertEqual(response.headers["Cache-Control"], "no-store")

    # Borde y fallos comunes

    def test_other_actor_or_no_token_never_reach_the_service(self) -> None:
        for actor in ("organizer", "service_provider"):
            for name, response in self.every_operation(self.auth(actor)).items():
                with self.subTest(operation=name, actor=actor):
                    self.assertEqual(
                        (response.status_code, response.json()["code"]),
                        (403, "permission_required"),
                    )
        for name, response in self.every_operation().items():
            with self.subTest(operation=name, token=None):
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.json()["code"], "invalid_access_token")
                self.assertEqual(response.headers["WWW-Authenticate"], "Bearer")
        invalid = self.every_operation({"Authorization": "Bearer no-es-un-token"})
        for name, response in invalid.items():
            with self.subTest(operation=name, token="invalid"):
                self.assertEqual(response.status_code, 401)
        self.assertEqual(self.requests, [])
        self.assertEqual(self.upstream.affiliation_paths, [])

    def test_jwks_unavailable_is_503(self) -> None:
        self.upstream.jwks_down = True
        for name, response in self.every_operation(self.auth()).items():
            with self.subTest(operation=name):
                self.assertEqual(
                    (response.status_code, response.json()["code"]), (503, "upstream_unavailable")
                )

    def test_service_unreachable_is_503_or_504(self) -> None:
        self.failure = httpx.ReadTimeout
        timeout = self.every_operation(self.auth())
        self.failure = httpx.ConnectError
        down = self.every_operation(self.auth())
        for name in timeout:
            with self.subTest(operation=name):
                self.assertEqual(
                    (timeout[name].status_code, timeout[name].json()["code"]),
                    (504, "upstream_timeout"),
                )
                self.assertEqual(
                    (down[name].status_code, down[name].json()["code"]),
                    (503, "upstream_unavailable"),
                )

    def test_unexpected_service_status_or_body_is_503(self) -> None:
        for reply in (
            httpx.Response(500, json=error("internal_error")),
            httpx.Response(404, json=error("not_found")),
            httpx.Response(201, content=b"<html>"),
        ):
            self.reply = reply
            response = self.book(self.auth())
            with self.subTest(status=reply.status_code):
                self.assertEqual(
                    (response.status_code, response.json()["code"]), (503, "upstream_unavailable")
                )
        self.reply = httpx.Response(409, json=error("availability_full"))
        for name, response in self.every_operation(self.auth()).items():
            if name == "book":
                continue
            with self.subTest(operation=name, status=409):
                self.assertEqual(response.status_code, 503)


if __name__ == "__main__":
    unittest.main()
