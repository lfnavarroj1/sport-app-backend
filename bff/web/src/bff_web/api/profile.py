"""HU005 en el canal web: perfil demográfico y fisiológico del deportista (ADR-034).

El BFF autentica, aplica el filtro de borde por actor (`athlete`, sin capacidad de plan)
y reenvía el cuerpo, la clave de idempotencia y el token sin reinterpretarlos: las reglas
del perfil, el consentimiento de datos de salud, la concurrencia por `expected_version` y
los errores `422 validation_error` con `details[{field, issue}]` los decide `users_management`
y se entregan sin alterar. Los datos del perfil son sensibles: nunca se registran (solo el
estado inesperado del servicio) y ninguna respuesta se guarda en caché (`no-store`). Los
instantes llegan en UTC (`Z`); la conversión a la zona del usuario corresponde al frontend.
"""

from collections.abc import Callable
from datetime import date, datetime
from http import HTTPStatus
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Header, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from bff_web.api.training import (
    _UNAUTHORIZED,
    _UPSTREAM,
    ATHLETE,
    _passthrough,
    _permission_required,
    _Session,
    _upstream_failure,
    authenticate_session,
)
from bff_web.clients.users_management import (
    UpstreamTimeout,
    UpstreamUnavailable,
    UsersManagementClient,
)
from sportapp_platform.errors import ErrorResponse
from sportapp_platform.tokens import VerifiedAccessToken

SOURCE = "users-management"
IDEMPOTENCY_HEADER = "Idempotency-Key"
REPLAYED_HEADER = "Idempotent-Replayed"
CACHE_CONTROL_HEADER = "Cache-Control"
TRIMMED = "Se recortan los espacios exteriores."
UTC_INSTANT = "Instante en UTC con sufijo `Z`."
REQUEST_DECIMAL = r"^\d{1,3}(\.\d)?$"
RESPONSE_DECIMAL = r"^\d{1,3}\.\d$"

Gender = Literal["FEMALE", "MALE", "NON_BINARY", "PREFER_NOT_TO_SAY"]
MaritalStatus = Literal[
    "SINGLE", "MARRIED", "DOMESTIC_PARTNERSHIP", "DIVORCED", "WIDOWED", "PREFER_NOT_TO_SAY"
]
IncomeRange = Literal["LT_1_SMMLV", "1_2_SMMLV", "2_4_SMMLV", "GT_4_SMMLV", "PREFER_NOT_TO_SAY"]

ExpectedVersion = Annotated[
    int | None,
    Field(
        ge=1,
        description="Se omite (o `null`) al crear; obligatorio e igual a la versión vigente al "
        "reemplazar.",
    ),
]
CountryCode = Annotated[
    str,
    Field(
        pattern=r"^[A-Z]{2}$",
        description="ISO 3166-1 alfa-2 en mayúsculas (catálogo de ADR-028).",
        examples=["CO"],
    ),
]
City = Annotated[str, Field(min_length=2, max_length=80, description=TRIMMED)]
Occupation = Annotated[str, Field(min_length=2, max_length=80, description=TRIMMED)]
HeightCm = Annotated[int, Field(ge=100, le=250)]
RestingHeartRate = Annotated[int, Field(ge=30, le=220)]


class BloodPressure(BaseModel):
    """Presión arterial en mmHg; la sistólica debe ser mayor que la diastólica."""

    model_config = ConfigDict(extra="forbid")

    systolic_mmhg: Annotated[int, Field(ge=60, le=250)]
    diastolic_mmhg: Annotated[int, Field(ge=30, le=150)]


class HealthConsentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Annotated[
        str,
        Field(
            description="Versión vigente del consentimiento de datos de salud "
            "(`HEALTH_DATA_CONSENT_VERSION`); otra → `unsupported_consent_version`.",
            examples=["health-2026-10-12.v1"],
        ),
    ]


class DemographicProfileRequest(BaseModel):
    """Esquema documentado del cuerpo; la validación la hace el servicio (ADR-034).

    No admite `user_id` ni otros campos (`field_not_allowed`).
    """

    model_config = ConfigDict(extra="forbid")

    expected_version: ExpectedVersion = None
    birth_date: Annotated[
        date, Field(description="Fecha ISO; edad entre 13 y 120 años en la fecha del comando.")
    ]
    gender: Gender
    country_code: CountryCode
    city: City
    marital_status: MaritalStatus | None = None
    occupation: Occupation | None = None
    income_range: IncomeRange | None = None


class PhysiologicalProfileRequest(BaseModel):
    """Esquema documentado del cuerpo; la validación la hace el servicio (ADR-034).

    No admite `user_id`, `bmi` (lo calcula el backend) ni otros campos (`field_not_allowed`).
    Los decimales viajan como cadena; un número JSON en su lugar → `unknown_value`.
    """

    model_config = ConfigDict(extra="forbid")

    expected_version: ExpectedVersion = None
    consent: HealthConsentRequest
    measured_at: Annotated[
        str,
        Field(
            pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$",
            description="UTC con sufijo `Z` y precisión de segundos; hasta 5 minutos en el "
            "futuro y 365 días en el pasado.",
            examples=["2026-10-12T13:00:00Z"],
        ),
    ]
    weight_kg: Annotated[
        str,
        Field(
            pattern=REQUEST_DECIMAL,
            description="Cadena decimal con hasta un decimal, de 20.0 a 300.0.",
            examples=["68.5"],
        ),
    ]
    height_cm: HeightCm
    resting_heart_rate_bpm: RestingHeartRate | None = None
    blood_pressure: BloodPressure | None = None
    body_temperature_c: Annotated[
        str | None,
        Field(
            pattern=REQUEST_DECIMAL,
            description="Cadena decimal con hasta un decimal, de 34.0 a 42.0.",
            examples=["36.6"],
        ),
    ] = None


class _Owner(BaseModel):
    user_id: UUID


class DemographicProfileSection(BaseModel):
    """Sección demográfica normalizada (sin raza/etnia ni religión)."""

    version: int
    birth_date: date
    gender: Gender
    country_code: str
    city: str
    marital_status: MaritalStatus | None
    occupation: str | None
    income_range: IncomeRange | None
    created_at: Annotated[datetime, Field(description=UTC_INSTANT)]
    updated_at: Annotated[datetime, Field(description=UTC_INSTANT)]


class HealthConsent(BaseModel):
    version: str
    accepted_at: Annotated[
        datetime, Field(description="Instante del registro del consentimiento, en UTC (`Z`).")
    ]


class PhysiologicalProfileSection(BaseModel):
    """Sección fisiológica normalizada; sin rangos clínicos ni etiquetas de riesgo."""

    version: int
    consent: HealthConsent
    measured_at: Annotated[datetime, Field(description=UTC_INSTANT)]
    weight_kg: Annotated[
        str, Field(pattern=RESPONSE_DECIMAL, description="Un decimal.", examples=["68.5"])
    ]
    height_cm: int
    bmi: Annotated[
        str,
        Field(
            pattern=RESPONSE_DECIMAL,
            description="Calculado por el backend: peso / altura², un decimal, `ROUND_HALF_UP`.",
            examples=["23.2"],
        ),
    ]
    resting_heart_rate_bpm: int | None
    blood_pressure: BloodPressure | None
    body_temperature_c: Annotated[
        str | None, Field(pattern=RESPONSE_DECIMAL, description="Un decimal.", examples=["36.6"])
    ]
    created_at: Annotated[datetime, Field(description=UTC_INSTANT)]
    updated_at: Annotated[datetime, Field(description=UTC_INSTANT)]


# Con `_Owner` en segundo lugar, Pydantic coloca `user_id` antes de los campos de la sección.
class DemographicProfileResponse(DemographicProfileSection, _Owner):
    """Respuesta del comando: la sección con el `user_id` del token."""


class PhysiologicalProfileResponse(PhysiologicalProfileSection, _Owner):
    """Respuesta del comando: la sección con el `user_id` del token."""


class ProfileVersions(BaseModel):
    demographic: int | None
    physiological: int | None
    sport_habits: int | None


class ProfileResponse(BaseModel):
    """Perfil proyectado (consistencia eventual): `null` en las secciones aún sin datos."""

    user_id: UUID
    demographic: DemographicProfileSection | None
    physiological: PhysiologicalProfileSection | None
    sport_habits: Annotated[
        dict[str, Any] | None, Field(description="HU006; siempre `null` en esta versión.")
    ]
    versions: ProfileVersions
    projected_at: Annotated[
        datetime | None, Field(description="Última proyección aplicada, en UTC (`Z`).")
    ]


def _inline_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Esquema autocontenido del cuerpo: los modelos anidados que solo usa la solicitud
    (p. ej. el consentimiento) no existen en `components` y no pueden quedar como `$ref`."""
    schema = model.model_json_schema()
    definitions = schema.pop("$defs", {})

    def resolve(node: Any) -> Any:
        if isinstance(node, list):
            return [resolve(item) for item in node]
        if not isinstance(node, dict):
            return node
        resolved = {key: resolve(value) for key, value in node.items() if key != "$ref"}
        reference = node.get("$ref")
        if isinstance(reference, str) and reference.startswith("#/$defs/"):
            return {**resolve(definitions[reference.removeprefix("#/$defs/")]), **resolved}
        return resolved

    resolved_schema: dict[str, Any] = resolve(schema)
    return resolved_schema


def _request_body(model: type[BaseModel]) -> dict[str, Any]:
    return {
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": _inline_schema(model)}},
        }
    }


_CACHE_HEADER_DOC = {
    CACHE_CONTROL_HEADER: {"description": "`no-store`.", "schema": {"type": "string"}}
}
_COMMAND_HEADERS_DOC = {
    REPLAYED_HEADER: {
        "description": "`true` cuando se devuelve el resultado de un reintento.",
        "schema": {"type": "string"},
    },
    **_CACHE_HEADER_DOC,
}
_FORBIDDEN: dict[int | str, dict[str, Any]] = {
    HTTPStatus.FORBIDDEN.value: {
        "model": ErrorResponse,
        "description": "`permission_required`: solo el deportista (`athlete`).",
    }
}
_COMMAND_ERRORS: dict[int | str, dict[str, Any]] = {
    **_UNAUTHORIZED,
    **_FORBIDDEN,
    **_UPSTREAM,
    HTTPStatus.CONFLICT.value: {
        "model": ErrorResponse,
        "description": '`version_conflict` con `details=[{field: "expected_version", issue: '
        '"<versión vigente>"}]` (`"0"` si la sección no existe), o `idempotency_key_reused` '
        "(la clave ya se usó con otro cuerpo o por otro deportista).",
    },
    HTTPStatus.UNPROCESSABLE_ENTITY.value: {
        "model": ErrorResponse,
        "description": "`validation_error` con `details[{field, issue}]` (catálogo de ADR-034); "
        "incluye `Idempotency-Key` ausente (`required`) o que no es un UUID (`unknown_value`). "
        "Todo o nada: no se guarda ningún dato parcial.",
    },
}
_IDEMPOTENCY_DESCRIPTION = "UUID; obligatorio (lo valida el servicio)."


def build_profile_router(
    *,
    verify_token: Callable[[str], VerifiedAccessToken],
    users: UsersManagementClient,
) -> APIRouter:
    router = APIRouter(tags=["profile"])

    def athlete_session(authorization: str | None) -> _Session | Response:
        session = authenticate_session(verify_token, authorization)
        if isinstance(session, Response):
            return session
        if session.actor_type != ATHLETE:
            return _permission_required()
        return session

    def private(response: Response) -> Response:
        response.headers[CACHE_CONTROL_HEADER] = "no-store"
        return response

    def put_section(
        send: Callable[[str, bytes, str | None], Any],
        body: bytes,
        idempotency_key: str | None,
        authorization: str | None,
    ) -> Response:
        session = athlete_session(authorization)
        if isinstance(session, Response):
            return private(session)
        try:
            upstream = send(session.token, body, idempotency_key)
        except (UpstreamTimeout, UpstreamUnavailable) as error:
            return private(_upstream_failure(error))
        return private(
            _passthrough(
                upstream,
                {
                    HTTPStatus.OK,
                    HTTPStatus.CREATED,
                    HTTPStatus.UNAUTHORIZED,
                    HTTPStatus.FORBIDDEN,
                    HTTPStatus.CONFLICT,
                    HTTPStatus.UNPROCESSABLE_ENTITY,
                },
                headers=(REPLAYED_HEADER, CACHE_CONTROL_HEADER),
                source=SOURCE,
            )
        )

    @router.put(
        "/v1/me/demographic-profile",
        response_model=DemographicProfileResponse,
        summary="Registrar o reemplazar mi perfil demográfico (HU005, ADR-034)",
        openapi_extra=_request_body(DemographicProfileRequest),
        responses={
            HTTPStatus.OK.value: {
                "description": "Sección reemplazada (o reintento de un reemplazo).",
                "headers": _COMMAND_HEADERS_DOC,
            },
            HTTPStatus.CREATED.value: {
                "model": DemographicProfileResponse,
                "description": "Sección creada (o reintento de la creación).",
                "headers": _COMMAND_HEADERS_DOC,
            },
            **_COMMAND_ERRORS,
        },
    )
    async def put_demographic(
        request: Request,
        idempotency_key: Annotated[
            str | None, Header(alias=IDEMPOTENCY_HEADER, description=_IDEMPOTENCY_DESCRIPTION)
        ] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        # Bytes crudos: el BFF no interpreta el JSON (ni siquiera uno mal formado) y
        # autentica antes de cualquier validación; lo bloqueante corre en el pool.
        return await run_in_threadpool(
            put_section,
            users.put_demographic_profile,
            await request.body(),
            idempotency_key,
            authorization,
        )

    @router.put(
        "/v1/me/physiological-profile",
        response_model=PhysiologicalProfileResponse,
        summary="Registrar o reemplazar mi perfil fisiológico (HU005, ADR-034)",
        description="Exige el consentimiento vigente de datos de salud; sin él no se guarda "
        "nada. El perfil no clasifica ni diagnostica.",
        openapi_extra=_request_body(PhysiologicalProfileRequest),
        responses={
            HTTPStatus.OK.value: {
                "description": "Sección reemplazada (o reintento de un reemplazo).",
                "headers": _COMMAND_HEADERS_DOC,
            },
            HTTPStatus.CREATED.value: {
                "model": PhysiologicalProfileResponse,
                "description": "Sección creada (o reintento de la creación).",
                "headers": _COMMAND_HEADERS_DOC,
            },
            **_COMMAND_ERRORS,
        },
    )
    async def put_physiological(
        request: Request,
        idempotency_key: Annotated[
            str | None, Header(alias=IDEMPOTENCY_HEADER, description=_IDEMPOTENCY_DESCRIPTION)
        ] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        return await run_in_threadpool(
            put_section,
            users.put_physiological_profile,
            await request.body(),
            idempotency_key,
            authorization,
        )

    @router.get(
        "/v1/me/profile",
        response_model=ProfileResponse,
        summary="Mi perfil de deportista (HU005, ADR-034)",
        description="Consulta la proyección: es eventual. Tras un comando, si la versión "
        "proyectada es menor que la confirmada, el cliente conserva la respuesta del comando y "
        "reintenta la consulta.",
        responses={
            HTTPStatus.OK.value: {"headers": _CACHE_HEADER_DOC},
            **_UNAUTHORIZED,
            **_FORBIDDEN,
            **_UPSTREAM,
            HTTPStatus.SERVICE_UNAVAILABLE.value: {
                "model": ErrorResponse,
                "description": "`upstream_unavailable`: el servicio o su base de lectura no "
                "están disponibles.",
            },
        },
    )
    def get_profile(authorization: Annotated[str | None, Header()] = None) -> Response:
        session = athlete_session(authorization)
        if isinstance(session, Response):
            return private(session)
        try:
            upstream = users.get_profile(session.token)
        except (UpstreamTimeout, UpstreamUnavailable) as error:
            return private(_upstream_failure(error))
        return private(
            _passthrough(
                upstream,
                {
                    HTTPStatus.OK,
                    HTTPStatus.UNAUTHORIZED,
                    HTTPStatus.FORBIDDEN,
                    HTTPStatus.SERVICE_UNAVAILABLE,
                },
                headers=(CACHE_CONTROL_HEADER,),
                source=SOURCE,
            )
        )

    return router
