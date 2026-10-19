"""BFF web, HU010: autenticación en el borde, resolución de correos y reenvío al servicio.

Usuarios (JWKS y lookup) y Entrenamientos se simulan con `httpx.MockTransport`.
"""

import json
import time
import unittest
import uuid
from typing import Any

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from bff_web.main import create_app
from bff_web.settings import BffWebSettings
from sportapp_platform.tokens import public_jwk

ISSUER = "sportapp-users-management"
AUDIENCE = "sportapp"
SIGNER = Ed25519PrivateKey.generate()
ATHLETE_ID, PROFESSIONAL_ID = uuid.uuid4(), uuid.uuid4()
DIRECTORY = {
    "ana@example.com": {"user_id": str(ATHLETE_ID), "actor_type": "athlete"},
    "pro@example.com": {"user_id": str(PROFESSIONAL_ID), "actor_type": "service_provider"},
    "org@example.com": {"user_id": str(uuid.uuid4()), "actor_type": "organizer"},
}


def token(actor_type: str | None, signer: Ed25519PrivateKey = SIGNER) -> str:
    now = int(time.time())
    claims: dict[str, Any] = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": str(uuid.uuid4()),
        "iat": now,
        "nbf": now,
        "exp": now + 900,
        "jti": str(uuid.uuid4()),
    }
    if actor_type is not None:
        claims["actor_type"] = actor_type
    return jwt.encode(
        claims, signer, algorithm="EdDSA", headers={"kid": public_jwk(signer.public_key())["kid"]}
    )


def plan_request(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "athlete_email": "ana@example.com",
        "title": "Base aeróbica",
        "objectives": ["Correr 10 km"],
        "start_date": "2026-11-02",
        "end_date": "2026-11-29",
        "sessions": [
            {
                "scheduled_date": "2026-11-02",
                "sport": "running",
                "duration_minutes": 40,
                "intensity_rpe": 5,
            }
        ],
    }
    values.update(overrides)
    return values


class Upstreams:
    def __init__(self) -> None:
        self.training_requests: list[httpx.Request] = []
        self.training_response = httpx.Response(201, json={"plan_id": str(uuid.uuid4())})
        self.lookup_status: int | None = None
        self.lookup_down = False
        self.training_down: type[Exception] | None = None

    def users(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/.well-known/jwks.json":
            return httpx.Response(200, json={"keys": [public_jwk(SIGNER.public_key())]})
        if self.lookup_down:
            raise httpx.ConnectError("usuarios caído")
        if self.lookup_status is not None:
            return httpx.Response(self.lookup_status)
        found = DIRECTORY.get(json.loads(request.content)["email"])
        return httpx.Response(200, json=found) if found else httpx.Response(404)

    def training(self, request: httpx.Request) -> httpx.Response:
        if self.training_down is not None:
            raise self.training_down("entrenamientos")
        self.training_requests.append(request)
        return self.training_response


class TrainingRoutesTest(unittest.TestCase):
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
                affiliation_http=httpx.Client(
                    transport=httpx.MockTransport(lambda _: httpx.Response(500)),
                    base_url="http://affiliation",
                ),
                training_http=httpx.Client(
                    transport=httpx.MockTransport(self.upstreams.training),
                    base_url="http://training",
                ),
            )
        )
        self.athlete_token = token("athlete")
        self.professional_token = token("service_provider")

    def headers(self, raw_token: str, **extra: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {raw_token}", **extra}

    def create(self, body: dict[str, Any] | None = None, actor: str = "service_provider") -> Any:
        return self.client.post(
            "/v1/training-plans",
            json=body or plan_request(),
            headers=self.headers(token(actor), **{"Idempotency-Key": str(uuid.uuid4())}),
        )

    def test_grant_resolves_professional_and_forwards_token(self) -> None:
        self.upstreams.training_response = httpx.Response(
            201,
            json={
                "athlete_id": str(ATHLETE_ID),
                "professional_id": str(PROFESSIONAL_ID),
                "status": "ACTIVE",
                "granted_at": "2026-10-12T12:00:00+00:00",
            },
        )

        response = self.client.post(
            "/v1/me/training-authorizations",
            json={"professional_email": "pro@example.com"},
            headers=self.headers(self.athlete_token),
        )

        self.assertEqual(response.status_code, 201, response.text)
        [forwarded] = self.upstreams.training_requests
        self.assertEqual(forwarded.method, "PUT")
        self.assertEqual(forwarded.url.path, f"/v1/training-authorizations/{PROFESSIONAL_ID}")
        self.assertEqual(forwarded.headers["Authorization"], f"Bearer {self.athlete_token}")

    def test_grant_to_unknown_or_non_professional_email_is_404(self) -> None:
        for email in ("nadie@example.com", "ana@example.com", "org@example.com"):
            with self.subTest(email=email):
                response = self.client.post(
                    "/v1/me/training-authorizations",
                    json={"professional_email": email},
                    headers=self.headers(self.athlete_token),
                )
                self.assertEqual(response.status_code, 404)
                self.assertEqual(response.json()["code"], "professional_not_found")
        self.assertEqual(self.upstreams.training_requests, [])

    def test_only_athletes_grant_or_revoke_at_the_edge(self) -> None:
        grant = self.client.post(
            "/v1/me/training-authorizations",
            json={"professional_email": "pro@example.com"},
            headers=self.headers(self.professional_token),
        )
        revoke = self.client.delete(
            f"/v1/me/training-authorizations/{PROFESSIONAL_ID}",
            headers=self.headers(self.professional_token),
        )

        self.assertEqual((grant.status_code, revoke.status_code), (403, 403))
        self.assertEqual(grant.json()["code"], "permission_required")
        self.assertEqual(self.upstreams.training_requests, [])

    def test_revoke_forwards_delete(self) -> None:
        self.upstreams.training_response = httpx.Response(204)

        response = self.client.delete(
            f"/v1/me/training-authorizations/{PROFESSIONAL_ID}",
            headers=self.headers(self.athlete_token),
        )

        self.assertEqual(response.status_code, 204)
        self.assertEqual(self.upstreams.training_requests[0].method, "DELETE")

    def test_create_plan_replaces_email_with_athlete_id(self) -> None:
        """El servicio recibe el identificador, nunca el correo del deportista."""
        self.upstreams.training_response = httpx.Response(
            201,
            json={"plan_id": "x"},
            headers={"Location": "/v1/training-plans/x", "Idempotent-Replayed": "true"},
        )

        response = self.create()

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.headers["Location"], "/v1/training-plans/x")
        self.assertEqual(response.headers["Idempotent-Replayed"], "true")
        forwarded = self.upstreams.training_requests[0]
        sent = json.loads(forwarded.content)
        self.assertEqual(sent["athlete_id"], str(ATHLETE_ID))
        self.assertNotIn("athlete_email", sent)
        self.assertIn("Idempotency-Key", forwarded.headers)

    def test_unknown_athlete_email_is_indistinguishable_from_missing_authorization(self) -> None:
        """CA2 sin enumeración: correo inexistente o de otro actor → mismo 403."""
        for email in ("nadie@example.com", "pro@example.com"):
            with self.subTest(email=email):
                response = self.create(plan_request(athlete_email=email))
                self.assertEqual(response.status_code, 403)
                self.assertEqual(response.json()["code"], "athlete_authorization_required")
        self.assertEqual(self.upstreams.training_requests, [])

    def test_actors_without_author_permission_are_rejected_at_the_edge(self) -> None:
        """CA2: deportista y organizador no tienen `training.plan.author`."""
        for actor in ("athlete", "organizer"):
            with self.subTest(actor=actor):
                response = self.create(actor=actor)
                self.assertEqual(response.json()["code"], "permission_required")
        self.assertEqual(self.upstreams.training_requests, [])

    def test_service_errors_pass_through(self) -> None:
        for status, code in (
            (403, "athlete_authorization_required"),
            (409, "idempotency_key_reused"),
            (422, "training_plan_invalid"),
        ):
            with self.subTest(status=status):
                self.upstreams.training_response = httpx.Response(
                    status,
                    json={"code": code, "message": "m", "correlation_id": None, "details": []},
                )
                response = self.create()
                self.assertEqual((response.status_code, response.json()["code"]), (status, code))

    def test_transport_validation_happens_in_the_bff(self) -> None:
        response = self.create(plan_request(sessions=[]))

        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.upstreams.training_requests, [])

    def test_get_plan_forwards_and_disables_cache(self) -> None:
        self.upstreams.training_response = httpx.Response(
            404, json={"code": "training_plan_not_found"}
        )

        response = self.client.get(
            f"/v1/training-plans/{uuid.uuid4()}", headers=self.headers(self.athlete_token)
        )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_authentication_errors(self) -> None:
        other = Ed25519PrivateKey.generate()
        for headers in ({}, {"Authorization": "Bearer x"}, self.headers(token("athlete", other))):
            with self.subTest(headers=list(headers)):
                response = self.client.get(f"/v1/training-plans/{uuid.uuid4()}", headers=headers)
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.headers["WWW-Authenticate"], "Bearer")

    def test_upstream_failures(self) -> None:
        self.upstreams.lookup_down = True
        lookup_down = self.create()
        self.upstreams.lookup_down = False
        self.upstreams.lookup_status = 500
        lookup_error = self.create()
        self.upstreams.lookup_status = None
        self.upstreams.training_down = httpx.ReadTimeout
        timeout = self.create()
        self.upstreams.training_down = httpx.ConnectError
        unavailable = self.client.get(
            f"/v1/training-plans/{uuid.uuid4()}", headers=self.headers(self.athlete_token)
        )
        self.upstreams.training_down = None
        self.upstreams.training_response = httpx.Response(500)
        with self.assertLogs("sportapp.bff", level="WARNING"):
            unexpected = self.create()

        self.assertEqual(lookup_down.status_code, 503)
        self.assertEqual(lookup_error.status_code, 503)
        self.assertEqual((timeout.status_code, timeout.json()["code"]), (504, "upstream_timeout"))
        self.assertEqual(unavailable.status_code, 503)
        self.assertEqual(unexpected.status_code, 503)


if __name__ == "__main__":
    unittest.main()
