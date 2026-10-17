"""Contrato HTTP del perfil (HU005, ADR-034) con los casos de uso reales y persistencia en
memoria; el token se firma y se verifica de verdad."""

import logging
import unittest
from datetime import UTC, datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fakes import (
    CONSENT_VERSION,
    FixedClock,
    InMemoryProfileReader,
    InMemoryProfileStore,
    demographic_body,
    physiological_body,
    update_demographic_with,
    update_physiological_with,
)
from fastapi.testclient import TestClient

from sportapp_platform.app import create_service_app
from sportapp_platform.config import ServiceSettings
from sportapp_platform.tokens import AccessTokenVerifier
from users_management.api.profile import build_profile_router
from users_management.application.ports import AthleteProfileView
from users_management.application.queries.get_profile import GetAthleteProfile
from users_management.infrastructure.security.access_tokens import Ed25519AccessTokenIssuer

ISSUER = "sportapp-users-management"
AUDIENCE = "sportapp"
TOKENS = Ed25519AccessTokenIssuer(
    Ed25519PrivateKey.generate(), issuer=ISSUER, audience=AUDIENCE, ttl=timedelta(minutes=15)
)
VERIFIER = AccessTokenVerifier(TOKENS.jwks(), issuer=ISSUER, audience=AUDIENCE)
DEMOGRAPHIC = "/v1/me/demographic-profile"
PHYSIOLOGICAL = "/v1/me/physiological-profile"
PROFILE = "/v1/me/profile"
# Valores sintéticos que no pueden aparecer dentro de un UUID ni de un instante.
SENSITIVE_VALUES = ("68.5", "36.6", "23.2", "Bogotá", "Ingeniera", "1992-04-18")


def bearer(user_id: UUID, actor_type: str = "athlete") -> dict[str, str]:
    token = TOKENS.issue(user_id, actor_type, datetime.now(UTC)).token
    return {"Authorization": f"Bearer {token}"}


class ProfileApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = InMemoryProfileStore()
        self.reader = InMemoryProfileReader()
        clock = FixedClock()
        app = create_service_app(
            service_name="users-management",
            title="t",
            api_version="0",
            settings=ServiceSettings(app_env="test"),  # type: ignore[call-arg]
            routers=[
                build_profile_router(
                    verify_token=VERIFIER.verify,
                    update_demographic=update_demographic_with(self.store, clock),
                    update_physiological=update_physiological_with(self.store, clock),
                    get_profile=GetAthleteProfile(self.reader),
                )
            ],
        )
        self.client = TestClient(app)
        self.user_id = uuid4()
        self.auth = bearer(self.user_id)

    def put(
        self,
        path: str,
        body: Any,
        key: str | None = "new",
        auth: dict[str, str] | None = None,
    ) -> Any:
        headers = dict(self.auth if auth is None else auth)
        if key == "new":
            key = str(uuid4())
        if key is not None:
            headers["Idempotency-Key"] = key
        return self.client.put(path, json=body, headers=headers)

    def details(self, response: Any) -> list[tuple[str | None, str]]:
        return [(item["field"], item["issue"]) for item in response.json()["details"]]

    # --- CA1: alta y reemplazo ---

    def test_demographic_201_then_200_with_normalized_representation(self) -> None:
        """CA1: `201` al crear y `200` al reemplazar, con `no-store` y sin `Idempotent-Replayed`."""
        created = self.put(DEMOGRAPHIC, demographic_body(city=" Bogotá "))

        self.assertEqual(created.status_code, 201, created.text)
        self.assertEqual(created.headers["Cache-Control"], "no-store")
        self.assertNotIn("Idempotent-Replayed", created.headers)
        body = created.json()
        self.assertEqual(
            body,
            {
                "user_id": str(self.user_id),
                "version": 1,
                "birth_date": "1992-04-18",
                "gender": "FEMALE",
                "country_code": "CO",
                "city": "Bogotá",
                "marital_status": "SINGLE",
                "occupation": "Ingeniera",
                "income_range": "2_4_SMMLV",
                "created_at": "2026-10-07T12:00:00Z",
                "updated_at": "2026-10-07T12:00:00Z",
            },
        )

        replaced = self.put(DEMOGRAPHIC, demographic_body(expected_version=1, marital_status=None))

        self.assertEqual(replaced.status_code, 200, replaced.text)
        self.assertEqual(replaced.json()["version"], 2)
        self.assertIsNone(replaced.json()["marital_status"])

    def test_physiological_201_with_bmi_and_consent(self) -> None:
        """CA1: `bmi` calculado por el backend y consentimiento con su instante."""
        response = self.put(PHYSIOLOGICAL, physiological_body(weight_kg="68"))

        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        body = response.json()
        self.assertEqual(body["user_id"], str(self.user_id))
        self.assertEqual(body["weight_kg"], "68.0")
        self.assertEqual(body["bmi"], "23.0")
        self.assertEqual(body["body_temperature_c"], "36.6")
        self.assertEqual(
            body["consent"], {"version": CONSENT_VERSION, "accepted_at": "2026-10-07T12:00:00Z"}
        )
        self.assertEqual(
            set(body),
            {
                "user_id",
                "version",
                "consent",
                "measured_at",
                "weight_kg",
                "height_cm",
                "bmi",
                "resting_heart_rate_bpm",
                "blood_pressure",
                "body_temperature_c",
                "created_at",
                "updated_at",
            },
        )

    def test_profile_is_bound_to_the_token_identity(self) -> None:
        """CA1: cada deportista tiene su propia sección."""
        other = uuid4()
        self.put(DEMOGRAPHIC, demographic_body())

        response = self.put(DEMOGRAPHIC, demographic_body(), auth=bearer(other))

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["user_id"], str(other))

    # --- CA2 y CA3: validación ---

    def test_bmi_and_user_id_are_rejected_without_persisting(self) -> None:
        """CA2: `bmi` y `user_id` en el cuerpo → `422 field_not_allowed`."""
        response = self.put(PHYSIOLOGICAL, physiological_body(bmi="23.2", user_id=str(uuid4())))

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["code"], "validation_error")
        self.assertEqual(
            self.details(response), [("bmi", "field_not_allowed"), ("user_id", "field_not_allowed")]
        )
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual((self.store.sections, self.store.outbox), ({}, []))

    def test_json_number_where_decimal_string_is_expected(self) -> None:
        response = self.put(PHYSIOLOGICAL, physiological_body(weight_kg=68.5))

        self.assertEqual(self.details(response), [("weight_kg", "unknown_value")])

    def test_inconsistent_pressure_and_out_of_range(self) -> None:
        """CA2: presión inconsistente y valores fuera de rango, todos a la vez."""
        response = self.put(
            PHYSIOLOGICAL,
            physiological_body(
                blood_pressure={"systolic_mmhg": 70, "diastolic_mmhg": 90}, height_cm=260
            ),
        )

        self.assertEqual(response.status_code, 422)
        self.assertEqual(
            self.details(response),
            [("height_cm", "out_of_range"), ("blood_pressure", "inconsistent_values")],
        )

    def test_consent_missing_or_outdated(self) -> None:
        """CA3: el error indica `consent.version`; no se persiste nada."""
        body = physiological_body()
        del body["consent"]
        missing = self.put(PHYSIOLOGICAL, body)
        outdated = self.put(PHYSIOLOGICAL, physiological_body(consent={"version": "v0"}))

        self.assertEqual(self.details(missing), [("consent.version", "required")])
        self.assertEqual(
            self.details(outdated), [("consent.version", "unsupported_consent_version")]
        )
        self.assertEqual(self.store.sections, {})

    def test_idempotency_key_problems_are_reported_with_body_violations(self) -> None:
        """La clave ausente o no UUID se informa junto con las violaciones del cuerpo."""
        missing = self.put(DEMOGRAPHIC, demographic_body(gender="X"), key=None)
        invalid = self.put(DEMOGRAPHIC, demographic_body(), key="no-es-uuid")

        self.assertEqual(missing.status_code, 422)
        self.assertEqual(
            self.details(missing),
            [("Idempotency-Key", "required"), ("gender", "unknown_value")],
        )
        self.assertEqual(self.details(invalid), [("Idempotency-Key", "unknown_value")])
        self.assertEqual(self.store.sections, {})

    def test_body_must_be_a_json_object(self) -> None:
        response = self.put(DEMOGRAPHIC, ["x"])

        self.assertEqual(self.details(response), [(None, "unknown_value")])

    def test_malformed_json_is_validated_after_authenticating(self) -> None:
        """CA2: un JSON ilegible no se adelanta a la autenticación y usa el catálogo."""
        for path in (DEMOGRAPHIC, PHYSIOLOGICAL):
            with self.subTest(path=path):
                anonymous = self.client.put(
                    path, content=b"{bad", headers={"Content-Type": "application/json"}
                )
                malformed = self.client.put(
                    path,
                    content=b"{bad",
                    headers={
                        **self.auth,
                        "Content-Type": "application/json",
                        "Idempotency-Key": str(uuid4()),
                    },
                )
                empty = self.client.put(
                    path, headers={**self.auth, "Idempotency-Key": str(uuid4())}
                )

                self.assertEqual(anonymous.status_code, 401)
                self.assertEqual(anonymous.headers["Cache-Control"], "no-store")
                self.assertEqual(malformed.status_code, 422)
                self.assertEqual(malformed.headers["Cache-Control"], "no-store")
                self.assertEqual(self.details(malformed), [(None, "unknown_value")])
                self.assertEqual(self.details(empty), [(None, "required")])
        deep = self.client.put(
            DEMOGRAPHIC,
            content=b"[" * 200_000,
            headers={**self.auth, "Idempotency-Key": str(uuid4())},
        )
        self.assertEqual(deep.status_code, 422)
        self.assertEqual(self.details(deep), [(None, "unknown_value")])
        self.assertEqual(self.store.sections, {})

    # --- CA5: actor ---

    def test_401_without_or_with_invalid_token(self) -> None:
        for auth in ({}, {"Authorization": "Bearer x.y.z"}, {"Authorization": "Basic abc"}):
            for method, path in (("PUT", DEMOGRAPHIC), ("PUT", PHYSIOLOGICAL), ("GET", PROFILE)):
                with self.subTest(auth=auth, path=path):
                    response = self.client.request(
                        method, path, json=demographic_body(), headers=auth
                    )
                    self.assertEqual(response.status_code, 401)
                    self.assertEqual(response.json()["code"], "invalid_access_token")
                    self.assertEqual(response.headers["WWW-Authenticate"], "Bearer")
                    self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_403_for_organizer_and_service_provider(self) -> None:
        """CA5: solo `athlete` registra y consulta el perfil."""
        for actor in ("organizer", "service_provider"):
            auth = bearer(uuid4(), actor)
            for method, path in (("PUT", DEMOGRAPHIC), ("PUT", PHYSIOLOGICAL), ("GET", PROFILE)):
                with self.subTest(actor=actor, path=path):
                    response = self.client.request(
                        method, path, json=physiological_body(), headers=auth
                    )
                    self.assertEqual(response.status_code, 403)
                    self.assertEqual(response.json()["code"], "permission_required")
        self.assertEqual(self.store.sections, {})

    # --- CA6: versión e idempotencia ---

    def test_version_conflict_details_the_current_version(self) -> None:
        """CA6: sin `expected_version` o distinta → `409` con la versión vigente."""
        self.put(DEMOGRAPHIC, demographic_body())

        missing = self.put(DEMOGRAPHIC, demographic_body(city="Cali"))
        stale = self.put(DEMOGRAPHIC, demographic_body(expected_version=7))
        new_section = self.put(PHYSIOLOGICAL, physiological_body(expected_version=1))

        for response, issue in ((missing, "1"), (stale, "1"), (new_section, "0")):
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.json()["code"], "version_conflict")
            self.assertEqual(self.details(response), [("expected_version", issue)])
        self.assertEqual(len(self.store.outbox), 1)

    def test_retry_returns_original_response_marked_as_replayed(self) -> None:
        """CA6: misma clave y payload → respuesta original con `Idempotent-Replayed`."""
        key = str(uuid4())
        first = self.put(PHYSIOLOGICAL, physiological_body(), key=key)

        retry = self.put(PHYSIOLOGICAL, physiological_body(), key=key)

        self.assertEqual(retry.status_code, 201)
        self.assertEqual(retry.headers["Idempotent-Replayed"], "true")
        self.assertEqual(retry.headers["Cache-Control"], "no-store")
        self.assertEqual(retry.json(), first.json())
        self.assertEqual(len(self.store.outbox), 1)

    def test_key_reused_with_other_payload_or_athlete(self) -> None:
        """CA6: otra solicitud u otro deportista con la clave → `409 idempotency_key_reused`."""
        key = str(uuid4())
        self.put(DEMOGRAPHIC, demographic_body(), key=key)

        other_payload = self.put(DEMOGRAPHIC, demographic_body(city="Cali"), key=key)
        other_athlete = self.put(DEMOGRAPHIC, demographic_body(), key=key, auth=bearer(uuid4()))

        for response in (other_payload, other_athlete):
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.json()["code"], "idempotency_key_reused")
        self.assertEqual(len(self.store.outbox), 1)

    # --- Consulta ---

    def test_get_without_projection_returns_nulls(self) -> None:
        response = self.client.get(PROFILE, headers=self.auth)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(
            response.json(),
            {
                "user_id": str(self.user_id),
                "demographic": None,
                "physiological": None,
                "sport_habits": None,
                "versions": {"demographic": None, "physiological": None, "sport_habits": None},
                "projected_at": None,
            },
        )

    def test_get_returns_projected_sections_of_the_caller(self) -> None:
        """CA5: el deportista consulta solo su propio perfil."""
        demographic = self.put(DEMOGRAPHIC, demographic_body()).json()
        physiological = self.put(PHYSIOLOGICAL, physiological_body()).json()
        sections = {
            name: {k: v for k, v in doc.items() if k != "user_id"}
            for name, doc in (("demographic", demographic), ("physiological", physiological))
        }
        self.reader.views[self.user_id] = AthleteProfileView(
            user_id=self.user_id,
            demographic=sections["demographic"],
            physiological=sections["physiological"],
            sport_habits=None,
            demographic_version=1,
            physiological_version=1,
            sport_habits_version=None,
            projected_at=datetime(2026, 10, 7, 7, 0, 1, tzinfo=UTC).astimezone(
                timezone(timedelta(hours=-5))
            ),
        )

        own = self.client.get(PROFILE, headers=self.auth)
        other = self.client.get(PROFILE, headers=bearer(uuid4()))

        self.assertEqual(own.status_code, 200)
        body = own.json()
        self.assertEqual(body["demographic"], sections["demographic"])
        self.assertEqual(body["physiological"], sections["physiological"])
        self.assertEqual(
            body["versions"], {"demographic": 1, "physiological": 1, "sport_habits": None}
        )
        self.assertEqual(body["projected_at"], "2026-10-07T07:00:01Z")
        self.assertIsNone(other.json()["demographic"])

    def test_get_degrades_to_503_when_the_read_model_is_down(self) -> None:
        self.reader.unavailable = True

        response = self.client.get(PROFILE, headers=self.auth)

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["code"], "upstream_unavailable")
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    # --- CA4: minimización ---

    def test_logs_and_audit_never_contain_profile_values(self) -> None:
        """CA4: ni los logs ni la auditoría contienen valores demográficos o de salud."""
        logger = logging.getLogger()
        with self.assertLogs(logger, level="DEBUG") as logs:
            self.put(DEMOGRAPHIC, demographic_body())
            self.put(PHYSIOLOGICAL, physiological_body())
            self.put(PHYSIOLOGICAL, physiological_body(weight_kg="301.5"))
            self.put(DEMOGRAPHIC, demographic_body())  # 409
            self.client.get(PROFILE, headers=self.auth)

        rendered = " ".join(logs.output) + " ".join(
            str(getattr(record, "fields", "")) for record in logs.records
        )
        for value in (*SENSITIVE_VALUES, "301.5"):
            self.assertNotIn(value, rendered)
        audit = [
            record.fields["result"]  # type: ignore[attr-defined]
            for record in logs.records
            if record.name == "sportapp.audit"
        ]
        self.assertEqual(audit, ["created", "created", "rejected", "rejected"])


if __name__ == "__main__":
    unittest.main()
