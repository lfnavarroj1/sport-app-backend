"""BFF web, HU005: perfil demográfico y fisiológico del deportista (ADR-034).

Filtro de borde por actor, reenvío literal del cuerpo, la clave, el token y la
correlación, respuestas del servicio sin alterar, `no-store` y minimización en logs.
Usuarios (JWKS y perfil) se simula con `httpx.MockTransport`; la app se crea una vez por
clase porque cada `httpx.Client()` es costoso en Windows.
"""

import json
import logging
import unittest
import uuid
from typing import Any, ClassVar

import httpx
from fastapi.testclient import TestClient
from test_training import SIGNER, token

from bff_web.main import create_app
from bff_web.settings import BffWebSettings
from sportapp_platform.correlation import CORRELATION_HEADER
from sportapp_platform.tokens import public_jwk

WEB_ORIGIN = "http://localhost:4200"
DEMOGRAPHIC = "/v1/me/demographic-profile"
PHYSIOLOGICAL = "/v1/me/physiological-profile"
PROFILE = "/v1/me/profile"
USER_ID = str(uuid.uuid4())

DEMOGRAPHIC_BODY = {
    "birth_date": "1992-04-18",
    "gender": "FEMALE",
    "country_code": "CO",
    "city": "Bogotá",
    "marital_status": "SINGLE",
    "occupation": "Ingeniera",
    "income_range": "2_4_SMMLV",
}
PHYSIOLOGICAL_BODY = {
    "expected_version": None,
    "consent": {"version": "health-2026-10-12.v1"},
    "measured_at": "2026-10-12T13:00:00Z",
    "weight_kg": "68.5",
    "height_cm": 172,
    "resting_heart_rate_bpm": 58,
    "blood_pressure": {"systolic_mmhg": 118, "diastolic_mmhg": 76},
    "body_temperature_c": "36.6",
}
DEMOGRAPHIC_SECTION = {
    "version": 1,
    **DEMOGRAPHIC_BODY,
    "created_at": "2026-10-12T13:00:01Z",
    "updated_at": "2026-10-12T13:00:01Z",
}
PHYSIOLOGICAL_SECTION = {
    "version": 1,
    "consent": {"version": "health-2026-10-12.v1", "accepted_at": "2026-10-12T13:00:01Z"},
    "measured_at": "2026-10-12T13:00:00Z",
    "weight_kg": "68.5",
    "height_cm": 172,
    "bmi": "23.2",
    "resting_heart_rate_bpm": 58,
    "blood_pressure": {"systolic_mmhg": 118, "diastolic_mmhg": 76},
    "body_temperature_c": "36.6",
    "created_at": "2026-10-12T13:00:01Z",
    "updated_at": "2026-10-12T13:00:01Z",
}
PROFILE_VIEW = {
    "user_id": USER_ID,
    "demographic": DEMOGRAPHIC_SECTION,
    "physiological": None,
    "sport_habits": None,
    "versions": {"demographic": 1, "physiological": None, "sport_habits": None},
    "projected_at": "2026-10-12T13:00:02Z",
}
# Valores sintéticos que nunca deben aparecer en los logs.
SENSITIVE_VALUES = ("1992-04-18", "Bogotá", "Ingeniera", "68.5", "36.6", "23.2", "SMMLV")
COMMANDS = ((DEMOGRAPHIC, DEMOGRAPHIC_BODY), (PHYSIOLOGICAL, PHYSIOLOGICAL_BODY))


def error(code: str, details: list[dict[str, str]] | None = None) -> dict[str, Any]:
    return {"code": code, "message": "m", "correlation_id": None, "details": details or []}


class _Users:
    """Usuarios simulado: sirve el JWKS y registra las llamadas al perfil."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.requests: list[httpx.Request] = []
        self.response = httpx.Response(201, json={"user_id": USER_ID, **DEMOGRAPHIC_SECTION})
        self.failure: type[Exception] | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/.well-known/jwks.json":
            return httpx.Response(200, json={"keys": [public_jwk(SIGNER.public_key())]})
        if self.failure is not None:
            raise self.failure("usuarios")
        self.requests.append(request)
        return self.response


class _Capture(logging.Handler):
    """Mensaje, argumentos y campos estructurados de cada registro (lo que se emite)."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.text: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        fields = getattr(record, "fields", None)
        self.text.append(f"{record.getMessage()} {record.args!r} {fields!r} {record.exc_text}")


class ProfileRoutesTest(unittest.TestCase):
    users: ClassVar[_Users]
    client: ClassVar[TestClient]

    @classmethod
    def setUpClass(cls) -> None:
        cls.users = _Users()
        cls.client = TestClient(
            create_app(
                BffWebSettings(
                    app_env="test",  # type: ignore[arg-type]
                    users_management_base_url="http://users.invalid",  # type: ignore[arg-type]
                    affiliation_management_base_url="http://affiliation.invalid",  # type: ignore[arg-type]
                    training_management_base_url="http://training.invalid",  # type: ignore[arg-type]
                    complementary_services_base_url="http://services.invalid",  # type: ignore[arg-type]
                    events_management_base_url="http://events.invalid",  # type: ignore[arg-type]
                    cors_allowed_origins=(WEB_ORIGIN,),
                ),
                users_http=httpx.Client(
                    transport=httpx.MockTransport(cls.users), base_url="http://users"
                ),
            )
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.client.close()

    def setUp(self) -> None:
        self.users.reset()

    def put(self, path: str, body: Any, actor: str | None = "athlete", **headers: str) -> Any:
        sent = dict(headers)
        if actor is not None:
            sent["Authorization"] = f"Bearer {token(actor)}"
        return self.client.put(path, json=body, headers=sent)

    def get(self, actor: str | None = "athlete", **headers: str) -> Any:
        sent = dict(headers)
        if actor is not None:
            sent["Authorization"] = f"Bearer {token(actor)}"
        return self.client.get(PROFILE, headers=sent)

    def call_all(self, actor: str | None) -> list[tuple[str, httpx.Response]]:
        return [
            (DEMOGRAPHIC, self.put(DEMOGRAPHIC, DEMOGRAPHIC_BODY, actor)),
            (PHYSIOLOGICAL, self.put(PHYSIOLOGICAL, PHYSIOLOGICAL_BODY, actor)),
            (PROFILE, self.get(actor)),
        ]

    def test_without_token_responds_401_without_calling_the_service(self) -> None:
        """CA5: sin sesión válida no se lee ni se escribe el perfil."""
        for path, response in self.call_all(None):
            with self.subTest(path=path):
                self.assertEqual(
                    (response.status_code, response.json()["code"]), (401, "invalid_access_token")
                )
                self.assertEqual(response.headers["WWW-Authenticate"], "Bearer")
                self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(self.users.requests, [])

    def test_invalid_token_responds_401(self) -> None:
        response = self.client.get(PROFILE, headers={"Authorization": "Bearer no-es-un-jwt"})

        self.assertEqual(
            (response.status_code, response.json()["code"]), (401, "invalid_access_token")
        )
        self.assertEqual(self.users.requests, [])

    def test_only_the_athlete_passes_the_edge_filter(self) -> None:
        """CA5: otro actor (o sin actor) → `403 permission_required` sin llamar al servicio."""
        for actor in ("organizer", "service_provider"):
            for path, response in self.call_all(actor):
                with self.subTest(actor=actor, path=path):
                    self.assertEqual(
                        (response.status_code, response.json()["code"]),
                        (403, "permission_required"),
                    )
                    self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(self.put(DEMOGRAPHIC, DEMOGRAPHIC_BODY, actor=None).status_code, 401)
        response = self.client.get(PROFILE, headers={"Authorization": f"Bearer {token(None)}"})
        self.assertEqual(
            (response.status_code, response.json()["code"]), (403, "permission_required")
        )
        self.assertEqual(self.users.requests, [])

    def test_commands_forward_body_key_token_and_correlation_verbatim(self) -> None:
        """CA1/CA2: el BFF no valida ni reinterpreta el cuerpo; las reglas son del servicio."""
        for path, body in COMMANDS:
            with self.subTest(path=path):
                self.users.reset()
                key, correlation_id = str(uuid.uuid4()), str(uuid.uuid4())
                # Campos prohibidos y un número donde va cadena: el `422` lo decide el servicio.
                sent_body = {**body, "user_id": "inyectado", "bmi": 99, "weight_kg": 68.5}
                access_token = token("athlete")

                self.client.put(
                    path,
                    json=sent_body,
                    headers={
                        "Authorization": f"Bearer {access_token}",
                        "Idempotency-Key": key,
                        CORRELATION_HEADER: correlation_id,
                    },
                )

                [forwarded] = self.users.requests
                self.assertEqual((forwarded.method, forwarded.url.path), ("PUT", path))
                self.assertEqual(json.loads(forwarded.content), sent_body)
                self.assertEqual(forwarded.headers["Idempotency-Key"], key)
                self.assertEqual(forwarded.headers["Authorization"], f"Bearer {access_token}")
                self.assertEqual(forwarded.headers[CORRELATION_HEADER], correlation_id)

    def test_missing_or_invalid_key_is_forwarded_for_the_service_to_report(self) -> None:
        """La clave ausente o que no es UUID la informa el servicio junto con el cuerpo."""
        self.put(DEMOGRAPHIC, DEMOGRAPHIC_BODY)
        self.put(PHYSIOLOGICAL, PHYSIOLOGICAL_BODY, **{"Idempotency-Key": "no-es-uuid"})

        missing, invalid = self.users.requests
        self.assertNotIn("Idempotency-Key", missing.headers)
        self.assertEqual(invalid.headers["Idempotency-Key"], "no-es-uuid")

    def test_malformed_json_is_forwarded_as_bytes_after_authenticating(self) -> None:
        """CA2: el BFF no interpreta el cuerpo; sin sesión responde `401` antes de leerlo."""
        raw = '{"city": "Bogotá", bad'.encode()
        anonymous = self.client.put(
            DEMOGRAPHIC, content=raw, headers={"Content-Type": "application/json"}
        )
        forwarded = self.client.put(
            DEMOGRAPHIC,
            content=raw,
            headers={
                "Authorization": f"Bearer {token('athlete')}",
                "Content-Type": "application/json",
            },
        )

        self.assertEqual(anonymous.status_code, 401)
        self.assertEqual(anonymous.headers["Cache-Control"], "no-store")
        self.assertEqual(forwarded.status_code, 201)
        [request] = self.users.requests
        self.assertEqual(request.content, raw)
        self.assertEqual(request.headers["Content-Type"], "application/json")

    def test_missing_body_is_forwarded_empty(self) -> None:
        response = self.client.put(
            DEMOGRAPHIC, headers={"Authorization": f"Bearer {token('athlete')}"}
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(self.users.requests[0].content, b"")

    def test_command_successes_pass_through_with_headers_and_no_store(self) -> None:
        """CA1/CA6: `201` al crear, `200` al reemplazar y el reintento marcado."""
        demographic = {"user_id": USER_ID, **DEMOGRAPHIC_SECTION}
        physiological = {"user_id": USER_ID, **PHYSIOLOGICAL_SECTION}
        for (path, body), payload in zip(COMMANDS, (demographic, physiological), strict=True):
            for status, replayed in ((201, None), (200, None), (201, "true"), (200, "true")):
                with self.subTest(path=path, status=status, replayed=replayed):
                    headers = {"Cache-Control": "no-store"}
                    if replayed:
                        headers["Idempotent-Replayed"] = replayed
                    self.users.response = httpx.Response(status, json=payload, headers=headers)

                    response = self.put(path, body, **{"Idempotency-Key": str(uuid.uuid4())})

                    self.assertEqual((response.status_code, response.json()), (status, payload))
                    self.assertEqual(response.headers.get("Idempotent-Replayed"), replayed)
                    self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_command_errors_pass_through_unchanged(self) -> None:
        """CA2/CA3/CA6: validación, conflicto de versión y clave reutilizada sin alterar."""
        for status, payload in (
            (401, error("invalid_access_token")),
            (403, error("permission_required")),
            (409, error("version_conflict", [{"field": "expected_version", "issue": "3"}])),
            (409, error("version_conflict", [{"field": "expected_version", "issue": "0"}])),
            (409, error("idempotency_key_reused")),
            (
                422,
                error(
                    "validation_error",
                    [
                        {"field": "bmi", "issue": "field_not_allowed"},
                        {"field": "consent.version", "issue": "unsupported_consent_version"},
                        {"field": "blood_pressure", "issue": "inconsistent_values"},
                        {"field": "Idempotency-Key", "issue": "required"},
                    ],
                ),
            ),
        ):
            for path, body in COMMANDS:
                with self.subTest(status=status, code=payload["code"], path=path):
                    self.users.response = httpx.Response(status, json=payload)

                    response = self.put(path, body)

                    self.assertEqual((response.status_code, response.json()), (status, payload))
                    self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_unexpected_command_statuses_become_503(self) -> None:
        for status in (500, 503, 404, 204):
            for path, body in COMMANDS:
                with self.subTest(status=status, path=path):
                    self.users.response = httpx.Response(status, json=error("x"))
                    with self.assertLogs("sportapp.bff", level="WARNING"):
                        response = self.put(path, body)

                    self.assertEqual(
                        (response.status_code, response.json()["code"]),
                        (503, "upstream_unavailable"),
                    )
                    self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_timeouts_and_transport_errors(self) -> None:
        for failure, status, code in (
            (httpx.ReadTimeout, 504, "upstream_timeout"),
            (httpx.ConnectError, 503, "upstream_unavailable"),
        ):
            self.users.failure = failure
            for path, response in self.call_all("athlete"):
                with self.subTest(failure=failure.__name__, path=path):
                    self.assertEqual(
                        (response.status_code, response.json()["code"]), (status, code)
                    )
                    self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_profile_query_forwards_token_and_passes_the_view_through(self) -> None:
        """CA7: el perfil proyectado llega sin alterar, con secciones `null` si faltan."""
        self.users.response = httpx.Response(
            200, json=PROFILE_VIEW, headers={"Cache-Control": "no-store"}
        )
        access_token = token("athlete")

        response = self.client.get(PROFILE, headers={"Authorization": f"Bearer {access_token}"})

        self.assertEqual((response.status_code, response.json()), (200, PROFILE_VIEW))
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        [forwarded] = self.users.requests
        self.assertEqual((forwarded.method, forwarded.url.path), ("GET", PROFILE))
        self.assertEqual(forwarded.headers["Authorization"], f"Bearer {access_token}")
        self.assertEqual(forwarded.content, b"")

    def test_profile_query_statuses(self) -> None:
        """La base de lectura caída llega como el `503` del servicio; lo demás es `503`."""
        for status, payload in (
            (401, error("invalid_access_token")),
            (403, error("permission_required")),
            (503, error("upstream_unavailable")),
        ):
            with self.subTest(status=status):
                self.users.response = httpx.Response(status, json=payload)
                response = self.get()
                self.assertEqual((response.status_code, response.json()), (status, payload))
                self.assertEqual(response.headers["Cache-Control"], "no-store")
        for status in (500, 404, 409, 422):
            with self.subTest(status=status):
                self.users.response = httpx.Response(status, json=error("x"))
                with self.assertLogs("sportapp.bff", level="WARNING"):
                    response = self.get()
                self.assertEqual(
                    (response.status_code, response.json()["code"]), (503, "upstream_unavailable")
                )

    def test_profile_values_never_reach_the_logs(self) -> None:
        """CA4: ni el cuerpo ni la respuesta del perfil se registran."""
        capture = _Capture()
        root = logging.getLogger()
        previous = root.level
        root.addHandler(capture)
        root.setLevel(logging.DEBUG)
        try:
            self.users.response = httpx.Response(
                201, json={"user_id": USER_ID, **PHYSIOLOGICAL_SECTION}
            )
            self.put(PHYSIOLOGICAL, PHYSIOLOGICAL_BODY, **{"Idempotency-Key": str(uuid.uuid4())})
            self.put(DEMOGRAPHIC, DEMOGRAPHIC_BODY, **{"Idempotency-Key": str(uuid.uuid4())})
            self.users.response = httpx.Response(200, json=PROFILE_VIEW)
            self.get()
            self.users.response = httpx.Response(500, json=PHYSIOLOGICAL_SECTION)
            self.put(PHYSIOLOGICAL, PHYSIOLOGICAL_BODY)
            self.get()
        finally:
            root.removeHandler(capture)
            root.setLevel(previous)

        logged = "\n".join(capture.text)
        self.assertIn("bff.upstream_unexpected_status", logged)
        for value in SENSITIVE_VALUES:
            with self.subTest(value=value):
                self.assertNotIn(value, logged)

    def test_cors_preflight_allows_put_with_idempotency_key(self) -> None:
        """El cliente Angular de otro origen puede enviar los `PUT` del perfil."""
        response = self.client.options(
            DEMOGRAPHIC,
            headers={
                "Origin": WEB_ORIGIN,
                "Access-Control-Request-Method": "PUT",
                "Access-Control-Request-Headers": "authorization,content-type,idempotency-key",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["access-control-allow-origin"], WEB_ORIGIN)
        self.assertIn("PUT", response.headers["access-control-allow-methods"])
        allowed = response.headers["access-control-allow-headers"].lower()
        for header in ("authorization", "content-type", "idempotency-key"):
            self.assertIn(header, allowed)

    def test_replayed_header_is_exposed_to_the_browser(self) -> None:
        self.users.response = httpx.Response(
            200,
            json={"user_id": USER_ID, **DEMOGRAPHIC_SECTION},
            headers={"Idempotent-Replayed": "true"},
        )

        response = self.put(DEMOGRAPHIC, DEMOGRAPHIC_BODY, Origin=WEB_ORIGIN)

        self.assertEqual(response.headers["access-control-allow-origin"], WEB_ORIGIN)
        self.assertIn(
            "idempotent-replayed", response.headers["access-control-expose-headers"].lower()
        )


if __name__ == "__main__":
    unittest.main()
