"""HU005: perfil demográfico y fisiológico del deportista (contrato de ADR-034).

- `PUT /v1/me/demographic-profile` y `PUT /v1/me/physiological-profile`: registran
  (`201`) o reemplazan (`200`) la sección completa.
- `GET /v1/me/profile`: lee la proyección `athlete_profile_view` (consulta eventual).

El cuerpo de los `PUT` se lee como JSON sin validación automática para responder con
el catálogo de ADR-034 (`details[{field, issue}]`); su esquema se documenta en el
OpenAPI. Todas las respuestas llevan `Cache-Control: no-store` (datos personales y de
salud). Los instantes van en UTC con sufijo `Z`; el frontend los convierte.
"""

import json
from collections.abc import Callable
from datetime import UTC, date, datetime
from http import HTTPStatus
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Header, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from sportapp_platform.errors import ErrorDetail, ErrorResponse, error_response
from users_management.api.identity import (
    AUTH_RESPONSES,
    TokenVerifier,
    authenticate_athlete,
    correlation_uuid,
)
from users_management.api.profile_payload import parse_demographic, parse_physiological
from users_management.application.commands.profile_section import (
    ProfileSectionCommand,
    ProfileSectionResult,
    UpdateProfileSection,
)
from users_management.application.commands.update_demographic_profile import (
    UpdateDemographicProfile,
)
from users_management.application.commands.update_physiological_profile import (
    UpdatePhysiologicalProfile,
)
from users_management.application.errors import IdempotencyKeyReused, ReadModelUnavailable
from users_management.application.queries.get_profile import GetAthleteProfile
from users_management.domain.profile.demographic import (
    MAX_AGE_YEARS,
    MIN_AGE_YEARS,
    TEXT_MAX,
    TEXT_MIN,
    Gender,
    IncomeRange,
    MaritalStatus,
)
from users_management.domain.profile.physiological import (
    DIASTOLIC_MAX,
    DIASTOLIC_MIN,
    HEIGHT_MAX_CM,
    HEIGHT_MIN_CM,
    RESTING_HEART_RATE_MAX,
    RESTING_HEART_RATE_MIN,
    SYSTOLIC_MAX,
    SYSTOLIC_MIN,
)
from users_management.domain.profile.section import VersionConflict
from users_management.domain.profile.validation import InvalidProfileSection, Issue, Violation

IDEMPOTENCY_HEADER = "Idempotency-Key"
REPLAYED_HEADER = "Idempotent-Replayed"
NO_STORE = "no-store"
INVALID_REQUEST_MESSAGE = "La solicitud no es válida."
ONE_DECIMAL_PATTERN = r"^\d{1,3}\.\d$"
DECIMAL_INPUT_PATTERN = r"^\d{1,3}(\.\d)?$"
INSTANT_DESCRIPTION = "Instante en UTC con sufijo `Z`."


# --- Solicitudes (esquema documentado; la validación es del transporte y el dominio) ---


class DemographicProfileRequest(BaseModel):
    """Sección demográfica completa. `user_id` y cualquier campo no listado →
    `field_not_allowed`."""

    model_config = ConfigDict(extra="forbid")

    expected_version: Annotated[
        int | None,
        Field(ge=1, description="Se omite al crear; igual a la versión vigente al reemplazar."),
    ] = None
    birth_date: Annotated[
        date,
        Field(description=f"Edad entre {MIN_AGE_YEARS} y {MAX_AGE_YEARS} años (fecha UTC)."),
    ]
    gender: Gender
    country_code: Annotated[
        str, Field(pattern=r"^[A-Z]{2}$", description="ISO 3166-1 alfa-2 (ADR-028).")
    ]
    city: Annotated[
        str,
        Field(min_length=TEXT_MIN, max_length=TEXT_MAX, description="Se recortan los espacios."),
    ]
    marital_status: MaritalStatus | None = None
    occupation: Annotated[
        str | None,
        Field(min_length=TEXT_MIN, max_length=TEXT_MAX, description="Se recortan los espacios."),
    ] = None
    income_range: IncomeRange | None = None


class ConsentAcceptance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Annotated[
        str, Field(description="Igual a la versión vigente (`HEALTH_DATA_CONSENT_VERSION`).")
    ]


class BloodPressure(BaseModel):
    model_config = ConfigDict(extra="forbid")

    systolic_mmhg: Annotated[int, Field(ge=SYSTOLIC_MIN, le=SYSTOLIC_MAX)]
    diastolic_mmhg: Annotated[
        int, Field(ge=DIASTOLIC_MIN, le=DIASTOLIC_MAX, description="Menor que la sistólica.")
    ]


class PhysiologicalProfileRequest(BaseModel):
    """Sección fisiológica completa (datos de salud). `bmi` lo calcula el backend:
    enviarlo, igual que `user_id`, → `field_not_allowed`."""

    model_config = ConfigDict(extra="forbid")

    expected_version: Annotated[
        int | None,
        Field(ge=1, description="Se omite al crear; igual a la versión vigente al reemplazar."),
    ] = None
    consent: ConsentAcceptance
    measured_at: Annotated[
        str,
        Field(
            pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$",
            description="UTC `Z` con precisión de segundos; hasta 5 min en el futuro y "
            "365 días en el pasado.",
        ),
    ]
    weight_kg: Annotated[
        str,
        Field(
            pattern=DECIMAL_INPUT_PATTERN,
            description="Cadena decimal (0 o 1 decimal) entre 20.0 y 300.0.",
            examples=["68.5"],
        ),
    ]
    height_cm: Annotated[int, Field(ge=HEIGHT_MIN_CM, le=HEIGHT_MAX_CM)]
    resting_heart_rate_bpm: Annotated[
        int | None, Field(ge=RESTING_HEART_RATE_MIN, le=RESTING_HEART_RATE_MAX)
    ] = None
    blood_pressure: BloodPressure | None = None
    body_temperature_c: Annotated[
        str | None,
        Field(
            pattern=DECIMAL_INPUT_PATTERN,
            description="Cadena decimal (0 o 1 decimal) entre 34.0 y 42.0.",
            examples=["36.6"],
        ),
    ] = None


# --- Respuestas ---


class _UserIdentity(BaseModel):
    user_id: UUID


class DemographicSectionView(BaseModel):
    version: int
    birth_date: date
    gender: Gender
    country_code: str
    city: str
    marital_status: MaritalStatus | None
    occupation: str | None
    income_range: IncomeRange | None
    created_at: Annotated[datetime, Field(description=INSTANT_DESCRIPTION)]
    updated_at: Annotated[datetime, Field(description=INSTANT_DESCRIPTION)]


class DemographicProfileResponse(DemographicSectionView, _UserIdentity):
    pass


class HealthConsent(BaseModel):
    version: str
    accepted_at: Annotated[datetime, Field(description=INSTANT_DESCRIPTION)]


class PhysiologicalSectionView(BaseModel):
    version: int
    consent: HealthConsent
    measured_at: Annotated[datetime, Field(description=INSTANT_DESCRIPTION)]
    weight_kg: Annotated[str, Field(pattern=ONE_DECIMAL_PATTERN, examples=["68.5"])]
    height_cm: int
    bmi: Annotated[
        str,
        Field(
            pattern=ONE_DECIMAL_PATTERN,
            description="Estimado por el backend (peso / altura², ROUND_HALF_UP); sin "
            "clasificación clínica.",
            examples=["23.2"],
        ),
    ]
    resting_heart_rate_bpm: int | None
    blood_pressure: BloodPressure | None
    body_temperature_c: Annotated[str | None, Field(pattern=ONE_DECIMAL_PATTERN, examples=["36.6"])]
    created_at: Annotated[datetime, Field(description=INSTANT_DESCRIPTION)]
    updated_at: Annotated[datetime, Field(description=INSTANT_DESCRIPTION)]


class PhysiologicalProfileResponse(PhysiologicalSectionView, _UserIdentity):
    pass


class ProfileVersions(BaseModel):
    demographic: int | None
    physiological: int | None
    sport_habits: int | None


class AthleteProfileResponse(BaseModel):
    user_id: UUID
    demographic: DemographicSectionView | None
    physiological: PhysiologicalSectionView | None
    sport_habits: Annotated[
        dict[str, Any] | None, Field(description="HU006; `null` mientras no exista.")
    ]
    versions: ProfileVersions
    projected_at: Annotated[
        datetime | None,
        Field(description="Instante de la última proyección en UTC; `null` sin perfil."),
    ]


def _inline_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Esquema autocontenido: resuelve las referencias internas (`$defs`)."""
    schema = model.model_json_schema()
    definitions = schema.pop("$defs", {})

    def resolve(node: Any) -> Any:
        if isinstance(node, dict):
            reference = node.get("$ref")
            if isinstance(reference, str) and reference.startswith("#/$defs/"):
                return resolve(definitions[reference.removeprefix("#/$defs/")])
            return {name: resolve(value) for name, value in node.items()}
        if isinstance(node, list):
            return [resolve(item) for item in node]
        return node

    resolved: dict[str, Any] = resolve(schema)
    return resolved


def _request_body(model: type[BaseModel]) -> dict[str, Any]:
    return {
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": _inline_schema(model)}},
        }
    }


def _command_responses(model: type[BaseModel]) -> dict[int | str, dict[str, Any]]:
    replayed = {
        REPLAYED_HEADER: {
            "description": "`true` cuando se devuelve el resultado de un reintento.",
            "schema": {"type": "string"},
        }
    }
    return {
        **AUTH_RESPONSES,
        HTTPStatus.OK.value: {"description": "Sección reemplazada.", "headers": replayed},
        HTTPStatus.CREATED.value: {
            "model": model,
            "description": "Sección registrada.",
            "headers": replayed,
        },
        HTTPStatus.CONFLICT.value: {
            "model": ErrorResponse,
            "description": "`version_conflict` (`details[0].issue` = versión vigente, `0` si "
            "la sección no existe) o `idempotency_key_reused`.",
        },
        HTTPStatus.UNPROCESSABLE_ENTITY.value: {
            "model": ErrorResponse,
            "description": "`validation_error` con `details[{field, issue}]` (ADR-034).",
        },
    }


def _no_store(response: Response) -> Response:
    response.headers["Cache-Control"] = NO_STORE
    return response


def validation_failure(violations: list[Violation] | tuple[Violation, ...]) -> Response:
    return error_response(
        HTTPStatus.UNPROCESSABLE_ENTITY,
        InvalidProfileSection.code,
        INVALID_REQUEST_MESSAGE,
        [ErrorDetail(field=item.field, issue=item.issue.value) for item in violations],
    )


def parse_idempotency_key(raw: str | None) -> tuple[UUID | None, Violation | None]:
    if raw is None or not raw.strip():
        return None, Violation(IDEMPOTENCY_HEADER, Issue.REQUIRED)
    try:
        return UUID(raw.strip()), None
    except ValueError:
        return None, Violation(IDEMPOTENCY_HEADER, Issue.UNKNOWN_VALUE)


# Un cuerpo que no es JSON válido: el transporte lo informa como `unknown_value`.
_MALFORMED_JSON = object()


def decode_body(raw: bytes) -> object:
    """JSON del cuerpo, `None` si viene vacío o `_MALFORMED_JSON` si no se puede leer.

    Se decodifica a mano y después de autenticar: con `Body()` FastAPI respondería un `422`
    propio (`json_invalid`) antes de verificar el token y sin `Cache-Control: no-store`.
    """
    if not raw.strip():
        return None
    try:
        return json.loads(raw)
    except (ValueError, RecursionError):
        # `RecursionError`: anidamiento extremo; se trata igual que un JSON ilegible.
        return _MALFORMED_JSON


def _put_section[InputT](
    verify_token: TokenVerifier,
    use_case: UpdateProfileSection[InputT],
    *,
    parse: Callable[[object], InputT],
    raw_body: bytes,
    raw_key: str | None,
    authorization: str | None,
) -> Response:
    """Autenticar → actor → validar cuerpo y clave → idempotencia → versión."""
    user_id = authenticate_athlete(verify_token, authorization)
    if isinstance(user_id, Response):
        return _no_store(user_id)
    return _no_store(_run_command(use_case, user_id, parse(decode_body(raw_body)), raw_key))


def _run_command[InputT](
    use_case: UpdateProfileSection[InputT],
    user_id: UUID,
    section_input: InputT,
    raw_key: str | None,
) -> Response:
    key, key_problem = parse_idempotency_key(raw_key)
    try:
        if key is None:
            # La clave es inválida: se informan también las reglas del cuerpo.
            problems = [key_problem] if key_problem else []
            try:
                use_case.validate(section_input)
            except InvalidProfileSection as error:
                problems.extend(error.violations)
            return validation_failure(problems)
        result: ProfileSectionResult = use_case(
            user_id,
            ProfileSectionCommand(
                section_input=section_input, idempotency_key=key, correlation_id=correlation_uuid()
            ),
        )
    except InvalidProfileSection as error:
        return validation_failure(error.violations)
    except VersionConflict as error:
        return error_response(
            HTTPStatus.CONFLICT,
            error.code,
            "La sección cambió desde la última lectura; recargue y vuelva a intentar.",
            [ErrorDetail(field="expected_version", issue=str(error.current_version))],
        )
    except IdempotencyKeyReused as error:
        return error_response(
            HTTPStatus.CONFLICT,
            error.code,
            "La clave de idempotencia ya se usó con otra solicitud.",
        )
    response = JSONResponse(status_code=result.status, content=result.document)
    if result.replayed:
        response.headers[REPLAYED_HEADER] = "true"
    return response


def build_profile_router(
    *,
    verify_token: TokenVerifier,
    update_demographic: UpdateDemographicProfile,
    update_physiological: UpdatePhysiologicalProfile,
    get_profile: GetAthleteProfile,
) -> APIRouter:
    router = APIRouter(prefix="/v1/me", tags=["profile"])

    @router.put(
        "/demographic-profile",
        response_model=DemographicProfileResponse,
        summary="Registrar o reemplazar la sección demográfica (HU005)",
        openapi_extra=_request_body(DemographicProfileRequest),
        responses=_command_responses(DemographicProfileResponse),
    )
    async def put_demographic(
        request: Request,
        idempotency_key: Annotated[str | None, Header(alias=IDEMPOTENCY_HEADER)] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        # Solo se leen los bytes aquí; el trabajo bloqueante (token y base) corre en el pool.
        return await run_in_threadpool(
            _put_section,
            verify_token,
            update_demographic,
            parse=parse_demographic,
            raw_body=await request.body(),
            raw_key=idempotency_key,
            authorization=authorization,
        )

    @router.put(
        "/physiological-profile",
        response_model=PhysiologicalProfileResponse,
        summary="Registrar o reemplazar la sección fisiológica con consentimiento (HU005)",
        openapi_extra=_request_body(PhysiologicalProfileRequest),
        responses=_command_responses(PhysiologicalProfileResponse),
    )
    async def put_physiological(
        request: Request,
        idempotency_key: Annotated[str | None, Header(alias=IDEMPOTENCY_HEADER)] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        return await run_in_threadpool(
            _put_section,
            verify_token,
            update_physiological,
            parse=parse_physiological,
            raw_body=await request.body(),
            raw_key=idempotency_key,
            authorization=authorization,
        )

    @router.get(
        "/profile",
        response_model=AthleteProfileResponse,
        summary="Consultar el perfil propio desde la proyección (HU005)",
        responses={
            **AUTH_RESPONSES,
            HTTPStatus.SERVICE_UNAVAILABLE.value: {
                "model": ErrorResponse,
                "description": f"`{ReadModelUnavailable.code}`: la base de lectura no responde.",
            },
        },
    )
    def read_profile(
        response: Response,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response | AthleteProfileResponse:
        user_id = authenticate_athlete(verify_token, authorization)
        if isinstance(user_id, Response):
            return _no_store(user_id)
        try:
            view = get_profile(user_id)
        except ReadModelUnavailable as error:
            return _no_store(
                error_response(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    error.code,
                    "No fue posible consultar el perfil. Intente de nuevo.",
                )
            )
        response.headers["Cache-Control"] = NO_STORE
        return AthleteProfileResponse.model_validate(
            {
                "user_id": user_id,
                "demographic": view.demographic,
                "physiological": view.physiological,
                "sport_habits": view.sport_habits,
                "versions": {
                    "demographic": view.demographic_version,
                    "physiological": view.physiological_version,
                    "sport_habits": view.sport_habits_version,
                },
                "projected_at": (view.projected_at.astimezone(UTC) if view.projected_at else None),
            }
        )

    return router
