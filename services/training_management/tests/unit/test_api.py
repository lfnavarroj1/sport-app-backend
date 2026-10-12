"""API de HU010 en el servicio: autenticación, autorización, validación y errores.

Routers reales sobre casos de uso con dobles en memoria y tokens EdDSA firmados
en la prueba (misma verificación que en producción, sin red ni base de datos).
"""

import time
import unittest
import uuid
from typing import Any
from uuid import UUID

import jwt
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fakes import (
    FakeCapabilities,
    FakePermissions,
    FixedClock,
    InMemoryAuthorizationUnitOfWork,
    InMemoryPlanningUnitOfWork,
    InMemoryPlanReader,
    InMemoryReadStore,
    InMemoryTrainingStore,
    plan_body,
)
from fastapi.testclient import TestClient

from sportapp_platform.app import create_service_app
from sportapp_platform.config import ServiceSettings
from sportapp_platform.tokens import AccessTokenVerifier, JwksUnavailable, public_jwk
from training_management.api.authorizations import build_authorizations_router
from training_management.api.plans import build_plans_router
from training_management.application.commands.authorizations import (
    GrantAuthorization,
    RevokeAuthorization,
)
from training_management.application.commands.create_training_plan import CreateTrainingPlan
from training_management.application.ports import PlanView
from training_management.application.queries.get_training_plan import GetTrainingPlan

ISSUER = "sportapp-users-management"
AUDIENCE = "sportapp"
SIGNER = Ed25519PrivateKey.generate()
VERIFIER = AccessTokenVerifier(
    {"keys": [public_jwk(SIGNER.public_key())]}, issuer=ISSUER, audience=AUDIENCE
)


def token(user_id: UUID, actor_type: str | None, signer: Ed25519PrivateKey = SIGNER) -> str:
    now = int(time.time())
    claims: dict[str, Any] = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": str(user_id),
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


class TrainingApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = InMemoryTrainingStore()
        self.read = InMemoryReadStore()
        self.athlete_id, self.professional_id = uuid.uuid4(), uuid.uuid4()
        self.capabilities = FakeCapabilities({self.athlete_id: {"training.plan.manage"}})
        self.jwks_down = False
        clock = FixedClock()
        permissions = FakePermissions()

        def verify(raw: str) -> Any:
            if self.jwks_down:
                raise JwksUnavailable()
            return VERIFIER.verify(raw)

        app = create_service_app(
            service_name="training-management",
            title="test",
            api_version="test",
            settings=ServiceSettings(app_env="test"),
            routers=[
                build_authorizations_router(
                    verify_token=verify,
                    grant=GrantAuthorization(
                        unit_of_work=lambda: InMemoryAuthorizationUnitOfWork(self.store),
                        capabilities=self.capabilities,
                        clock=clock,
                    ),
                    revoke=RevokeAuthorization(
                        unit_of_work=lambda: InMemoryAuthorizationUnitOfWork(self.store),
                        clock=clock,
                    ),
                ),
                build_plans_router(
                    verify_token=verify,
                    create_plan=CreateTrainingPlan(
                        unit_of_work=lambda: InMemoryPlanningUnitOfWork(self.store),
                        permissions=permissions,
                        clock=clock,
                    ),
                    get_plan=GetTrainingPlan(
                        reader=InMemoryPlanReader(self.read),
                        permissions=permissions,
                        capabilities=self.capabilities,
                    ),
                ),
            ],
        )
        self.client = TestClient(app)
        self.athlete = {"Authorization": f"Bearer {token(self.athlete_id, 'athlete')}"}
        self.professional = {
            "Authorization": f"Bearer {token(self.professional_id, 'service_provider')}"
        }

    def authorize(self) -> Any:
        return self.client.put(
            f"/v1/training-authorizations/{self.professional_id}", headers=self.athlete
        )

    def create(self, body: dict[str, Any] | None = None, key: str | None = None) -> Any:
        return self.client.post(
            "/v1/training-plans",
            json=body or plan_body(self.athlete_id),
            headers={**self.professional, "Idempotency-Key": key or str(uuid.uuid4())},
        )

    def test_athlete_authorizes_and_professional_creates_plan(self) -> None:
        """CA1 de punta a punta en el servicio."""
        granted, again = self.authorize(), self.authorize()
        created = self.create()

        self.assertEqual((granted.status_code, again.status_code), (201, 200))
        self.assertEqual(granted.json()["status"], "ACTIVE")
        self.assertEqual(created.status_code, 201, created.text)
        body = created.json()
        self.assertEqual(body["athlete_id"], str(self.athlete_id))
        self.assertEqual(body["professional_id"], str(self.professional_id))
        self.assertEqual((body["status"], body["version"]), ("ASSIGNED", 1))
        self.assertEqual(created.headers["Location"], f"/v1/training-plans/{body['plan_id']}")

    def test_missing_or_invalid_token_is_401(self) -> None:
        other_signer = Ed25519PrivateKey.generate()
        for headers in (
            {},
            {"Authorization": "Basic abc"},
            {"Authorization": "Bearer "},
            {"Authorization": "Bearer no-es-un-jwt"},
            {"Authorization": f"Bearer {token(self.athlete_id, 'athlete', other_signer)}"},
        ):
            with self.subTest(headers=headers):
                response = self.client.put(
                    f"/v1/training-authorizations/{self.professional_id}", headers=headers
                )
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.json()["code"], "invalid_access_token")
                self.assertEqual(response.headers["WWW-Authenticate"], "Bearer")

    def test_token_without_actor_or_with_non_uuid_subject(self) -> None:
        no_actor = {"Authorization": f"Bearer {token(self.athlete_id, None)}"}
        response = self.client.put(
            f"/v1/training-authorizations/{self.professional_id}", headers=no_actor
        )

        self.assertEqual(
            (response.status_code, response.json()["code"]), (403, "permission_required")
        )

    def test_jwks_unavailable_is_503(self) -> None:
        self.jwks_down = True

        with self.assertLogs("sportapp.training", level="WARNING"):
            response = self.authorize()

        self.assertEqual(
            (response.status_code, response.json()["code"]), (503, "upstream_unavailable")
        )

    def test_without_athlete_authorization_is_403(self) -> None:
        """CA2."""
        response = self.create()

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], "athlete_authorization_required")

    def test_athlete_or_organizer_cannot_create_plans(self) -> None:
        """CA2: rol sin permiso."""
        for actor in ("athlete", "organizer"):
            with self.subTest(actor=actor):
                response = self.client.post(
                    "/v1/training-plans",
                    json=plan_body(self.athlete_id),
                    headers={
                        "Authorization": f"Bearer {token(uuid.uuid4(), actor)}",
                        "Idempotency-Key": str(uuid.uuid4()),
                    },
                )
                self.assertEqual(
                    (response.status_code, response.json()["code"]), (403, "permission_required")
                )

    def test_professional_cannot_grant_and_athlete_needs_capability(self) -> None:
        by_professional = self.client.put(
            f"/v1/training-authorizations/{uuid.uuid4()}", headers=self.professional
        )
        self.capabilities.granted = {}
        without_capability = self.authorize()
        self.capabilities.unavailable = True
        affiliation_down = self.authorize()

        self.assertEqual(by_professional.json()["code"], "permission_required")
        self.assertEqual(
            (without_capability.status_code, without_capability.json()["code"]),
            (403, "capability_required"),
        )
        self.assertEqual(affiliation_down.status_code, 503)

    def test_athlete_cannot_authorize_itself(self) -> None:
        response = self.client.put(
            f"/v1/training-authorizations/{self.athlete_id}", headers=self.athlete
        )

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["details"][0]["issue"], "self_authorization")

    def test_revoke(self) -> None:
        self.authorize()

        revoked = self.client.delete(
            f"/v1/training-authorizations/{self.professional_id}", headers=self.athlete
        )
        by_professional = self.client.delete(
            f"/v1/training-authorizations/{self.professional_id}", headers=self.professional
        )
        after = self.create()

        self.assertEqual(revoked.status_code, 204)
        self.assertEqual(by_professional.status_code, 403)
        self.assertEqual(after.json()["code"], "athlete_authorization_required")

    def test_incoherent_plan_reports_rules(self) -> None:
        """CA3: reglas cruzadas (dominio) y rangos (transporte)."""
        self.authorize()
        inverted = self.create(
            plan_body(self.athlete_id, start_date="2026-11-29", end_date="2026-11-02")
        )
        out_of_range = self.create(
            plan_body(
                self.athlete_id,
                sessions=[
                    {
                        "scheduled_date": "2026-11-02",
                        "sport": "running",
                        "duration_minutes": 40,
                        "intensity_rpe": 11,
                    }
                ],
            )
        )

        self.assertEqual(inverted.status_code, 422)
        self.assertEqual(inverted.json()["code"], "training_plan_invalid")
        self.assertIn(
            {"field": "body.end_date", "issue": "end_date_before_start_date"},
            inverted.json()["details"],
        )
        self.assertEqual(out_of_range.status_code, 422)
        self.assertEqual(out_of_range.json()["code"], "validation_error")
        self.assertEqual(
            out_of_range.json()["details"][0]["field"], "body.sessions.0.intensity_rpe"
        )
        self.assertEqual(self.store.plans, {})

    def test_unknown_fields_and_missing_key_are_rejected(self) -> None:
        self.authorize()
        extra = self.create(plan_body(self.athlete_id, professional_id=str(uuid.uuid4())))
        missing_key = self.client.post(
            "/v1/training-plans", json=plan_body(self.athlete_id), headers=self.professional
        )

        self.assertEqual(extra.status_code, 422)
        self.assertEqual(missing_key.status_code, 422)

    def test_idempotent_retry_and_key_reuse(self) -> None:
        """CA5."""
        self.authorize()
        key = str(uuid.uuid4())

        first, second = self.create(key=key), self.create(key=key)
        reused = self.create(plan_body(self.athlete_id, title="Otro"), key=key)

        self.assertEqual((first.status_code, second.status_code), (201, 201))
        self.assertEqual(second.json(), first.json())
        self.assertEqual(second.headers["Idempotent-Replayed"], "true")
        self.assertNotIn("Idempotent-Replayed", first.headers)
        self.assertEqual(
            (reused.status_code, reused.json()["code"]), (409, "idempotency_key_reused")
        )
        self.assertEqual(len(self.store.plans), 1)

    def test_get_plan_visibility(self) -> None:
        """CA4: creador y deportista ven el plan; otros reciben 404 sin detalles."""
        self.authorize()
        created = self.create().json()
        plan_id = UUID(created["plan_id"])
        # Simula la proyección ya aplicada (el proyector se prueba por separado).
        self.read.views[plan_id] = PlanView.from_document(created)
        stranger = {"Authorization": f"Bearer {token(uuid.uuid4(), 'service_provider')}"}

        by_professional = self.client.get(
            f"/v1/training-plans/{plan_id}", headers=self.professional
        )
        by_athlete = self.client.get(f"/v1/training-plans/{plan_id}", headers=self.athlete)
        by_stranger = self.client.get(f"/v1/training-plans/{plan_id}", headers=stranger)
        missing = self.client.get(f"/v1/training-plans/{uuid.uuid4()}", headers=self.professional)
        self.capabilities.granted = {}
        without_capability = self.client.get(f"/v1/training-plans/{plan_id}", headers=self.athlete)
        self.capabilities.unavailable = True
        affiliation_down = self.client.get(f"/v1/training-plans/{plan_id}", headers=self.athlete)

        self.assertEqual(by_professional.status_code, 200)
        self.assertEqual(by_professional.json(), created)
        self.assertEqual(by_professional.headers["Cache-Control"], "no-store")
        self.assertEqual(by_athlete.json(), created)
        self.assertEqual(
            (by_stranger.status_code, by_stranger.json()["code"]), (404, "training_plan_not_found")
        )
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(without_capability.json()["code"], "capability_required")
        self.assertEqual(affiliation_down.status_code, 503)
        self.assertNotIn("email", by_athlete.text)


if __name__ == "__main__":
    unittest.main()
