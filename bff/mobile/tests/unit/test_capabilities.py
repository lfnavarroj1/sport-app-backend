"""BFF móvil, HU003 CA6-CA10: matriz de `GET /v1/me/capabilities` (ADR-024 y ADR-025)."""

import time
import unittest
import uuid
from typing import Any

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from bff_mobile.main import create_app
from bff_mobile.settings import BffMobileSettings
from sportapp_platform.tokens import public_jwk

ISSUER = "sportapp-users-management"
AUDIENCE = "sportapp"
SIGNER = Ed25519PrivateKey.generate()
FREE_CAPABILITIES = sorted(
    [
        "training.plan.manage",
        "training.routes.suggest",
        "events.suggest",
        "training.plans.suggest",
        "services.support.suggest",
        "nutrition.routines.suggest",
        "integrations.strava.connect",
        "training.session.record",
        "training.dashboard.view",
        "training.metrics.ftp_vo2max",
        "calendar.sport.view",
    ]
)
CHANNEL_PERMISSIONS = {
    "organizer": ["training.routes.recommend"],
    "service_provider": [
        "events.recommend",
        "nutrition.routine.recommend",
        "services.support.recommend",
        "training.plan.recommend",
    ],
}


def token(actor_type: object = "athlete", signer: Ed25519PrivateKey = SIGNER, **claims: Any) -> str:
    now = int(time.time())
    payload: dict[str, Any] = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": str(uuid.uuid4()),
        "iat": now,
        "nbf": now,
        "exp": now + 900,
        "jti": str(uuid.uuid4()),
        **claims,
    }
    if actor_type is not None:
        payload["actor_type"] = actor_type
    return jwt.encode(
        payload,
        signer,
        algorithm="EdDSA",
        headers={"kid": public_jwk(signer.public_key())["kid"]},
    )


class Upstream:
    """Transporte simulado: JWKS de Usuarios y contrato interno de Afiliación."""

    def __init__(self, snapshot: dict[str, Any] | None = None, status: int = 200) -> None:
        self.snapshot = snapshot or {"subscription_status": "NONE", "capabilities": []}
        self.status = status
        self.affiliation_paths: list[str] = []
        self.jwks_down = False

    def users(self, request: httpx.Request) -> httpx.Response:
        if self.jwks_down:
            raise httpx.ConnectError("users caído")
        return httpx.Response(200, json={"keys": [public_jwk(SIGNER.public_key())]})

    def affiliation(self, request: httpx.Request) -> httpx.Response:
        self.affiliation_paths.append(request.url.path)
        if self.status == 0:
            raise httpx.ConnectError("afiliación caída")
        return httpx.Response(self.status, json=self.snapshot)


def client_with(upstream: Upstream) -> TestClient:
    settings = BffMobileSettings(
        app_env="test",  # type: ignore[arg-type]
        users_management_base_url="http://users.invalid",  # type: ignore[arg-type]
        affiliation_management_base_url="http://affiliation.invalid",  # type: ignore[arg-type]
    )
    app = create_app(
        settings,
        users_http=httpx.Client(
            base_url="http://users.invalid", transport=httpx.MockTransport(upstream.users)
        ),
        affiliation_http=httpx.Client(
            base_url="http://affiliation.invalid",
            transport=httpx.MockTransport(upstream.affiliation),
        ),
    )
    return TestClient(app)


def get(client: TestClient, bearer: str | None) -> httpx.Response:
    headers = {"Authorization": f"Bearer {bearer}"} if bearer else {}
    return client.get("/v1/me/capabilities", headers=headers)


class AthleteCapabilitiesTest(unittest.TestCase):
    def test_active_athlete_gets_the_capabilities_of_the_plan(self) -> None:
        """CA7: el menú del deportista se construye con las capacidades vigentes."""
        upstream = Upstream(
            {
                "user_id": "x",
                "subscription_status": "ACTIVE",
                "plan_code": "FREE",
                "plan_version": 1,
                "catalog_version": "plans-2026-10-11.v1",
                "capabilities": FREE_CAPABILITIES,
            }
        )
        user_id = str(uuid.uuid4())

        response = get(client_with(upstream), token(sub=user_id))

        body = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(body["actor_type"], "athlete")
        self.assertEqual(body["subscription_status"], "ACTIVE")
        self.assertEqual(
            (body["plan_code"], body["plan_version"], body["catalog_version"]),
            ("FREE", 1, "plans-2026-10-11.v1"),
        )
        self.assertEqual(body["capabilities"], FREE_CAPABILITIES)
        self.assertEqual(body["permissions"], [])
        self.assertEqual(body["authorization_policy_version"], "actor-permissions.v1")
        self.assertEqual(
            upstream.affiliation_paths, [f"/v1/subscriptions/by-user/{user_id}/capabilities"]
        )

    def test_athlete_without_subscription_is_provisioning_without_capabilities(self) -> None:
        """CA8 de HU001: nunca se concede FREE por suposición."""
        response = get(client_with(Upstream()), token())

        body = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body["subscription_status"], "PROVISIONING")
        self.assertIsNone(body["plan_code"])
        self.assertEqual(body["capabilities"], [])

    def test_affiliation_failure_is_503_never_a_free_plan(self) -> None:
        for status in (0, 500, 404):
            with self.subTest(status=status):
                response = get(client_with(Upstream(status=status)), token())

                self.assertEqual(response.status_code, 503)
                self.assertEqual(response.json()["code"], "upstream_unavailable")
                self.assertNotIn("capabilities", response.json())


class OtherActorsCapabilitiesTest(unittest.TestCase):
    def test_organizer_and_provider_get_their_web_permissions_without_plan(self) -> None:
        """Canal móvil: solo los permisos de HU aprobadas para móvil; sin plan de deportista."""
        for actor, expected in CHANNEL_PERMISSIONS.items():
            with self.subTest(actor=actor):
                upstream = Upstream()

                response = get(client_with(upstream), token(actor))

                body = response.json()
                self.assertEqual(response.status_code, 200)
                self.assertEqual(body["actor_type"], actor)
                self.assertEqual(body["subscription_status"], "NOT_APPLICABLE")
                self.assertIsNone(body["plan_code"])
                self.assertEqual(body["capabilities"], [])
                self.assertEqual(body["permissions"], expected)
                self.assertEqual(upstream.affiliation_paths, [], "no se consulta Afiliación")

    def test_unknown_or_missing_actor_is_rejected_without_a_default_role(self) -> None:
        """CA10."""
        for actor in ("admin", "", None, 7):
            with self.subTest(actor=actor):
                upstream = Upstream()

                response = get(client_with(upstream), token(actor))

                self.assertEqual(response.status_code, 403)
                self.assertEqual(response.json()["code"], "unsupported_actor_type")
                self.assertNotIn("permissions", response.json())
                self.assertEqual(upstream.affiliation_paths, [])


class AuthenticationTest(unittest.TestCase):
    def test_missing_malformed_expired_or_forged_tokens_are_401(self) -> None:
        now = int(time.time())
        cases = {
            "sin cabecera": None,
            "no es jwt": "basura",
            "vencido": token(exp=now - 1),
            "otra clave": token(signer=Ed25519PrivateKey.generate()),
            "otra audiencia": token(aud="otra"),
            "sujeto no uuid": token(sub="no-uuid"),
        }
        client = client_with(Upstream())
        for name, bearer in cases.items():
            with self.subTest(name=name):
                response = get(client, bearer)

                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.json()["code"], "invalid_access_token")
                self.assertEqual(response.headers["WWW-Authenticate"], "Bearer")
        basic = client.get("/v1/me/capabilities", headers={"Authorization": "Basic abc"})
        self.assertEqual(basic.status_code, 401)

    def test_user_comes_from_the_token_not_from_the_request(self) -> None:
        upstream = Upstream()
        user_id = str(uuid.uuid4())

        get(client_with(upstream), token(sub=user_id))
        client_with(upstream).get(
            f"/v1/me/capabilities?user_id={uuid.uuid4()}",
            headers={"Authorization": f"Bearer {token(sub=user_id)}"},
        )

        self.assertTrue(all(user_id in path for path in upstream.affiliation_paths))

    def test_unavailable_jwks_is_503_not_401(self) -> None:
        upstream = Upstream()
        upstream.jwks_down = True

        response = get(client_with(upstream), token())

        self.assertEqual(response.status_code, 503)


if __name__ == "__main__":
    unittest.main()
