"""HU005, HU006, HU008 y HU009 en el canal web: perfil del deportista (ADR-034 y ADR-035).

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
# HU006: catálogos cerrados de ADR-034 (deportes del catálogo de HU010).
Sport = Literal["running", "cycling", "swimming", "strength", "other"]
Intensity = Literal["LOW", "MODERATE", "HIGH"]
Level = Literal["BEGINNER", "INTERMEDIATE", "ADVANCED"]
Day = Literal["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]
TimeSlot = Literal["MORNING", "AFTERNOON", "EVENING", "NIGHT"]
PrimaryGoal = Literal["HEALTH", "PERFORMANCE", "WEIGHT_MANAGEMENT", "RECREATION", "COMPETITION"]
# HU009: catálogos cerrados de ADR-035 (sin texto libre).
DietPattern = Literal["OMNIVORE", "VEGETARIAN", "VEGAN", "PESCATARIAN", "FLEXITARIAN"]
Restriction = Literal[
    "GLUTEN_FREE", "LACTOSE_FREE", "LOW_SODIUM", "LOW_SUGAR", "KETO", "HALAL", "KOSHER"
]
Allergy = Literal[
    "PEANUTS", "TREE_NUTS", "MILK", "EGGS", "WHEAT", "SOY", "FISH", "SHELLFISH", "SESAME"
]

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


class SportHabit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sport: Annotated[Sport, Field(description="Catálogo de deportes de HU010; sin repetir.")]
    frequency_per_week: Annotated[int, Field(ge=1, le=7)]
    session_duration_minutes: Annotated[int, Field(ge=10, le=600)]
    intensity: Intensity
    level: Level
    years_practicing: Annotated[int | None, Field(ge=0, le=80)] = None


class SportAvailability(BaseModel):
    model_config = ConfigDict(extra="forbid")

    days: Annotated[list[Day], Field(min_length=1, max_length=7, description="Valores únicos.")]
    time_slots: Annotated[
        list[TimeSlot], Field(min_length=1, max_length=4, description="Valores únicos.")
    ]


class SportHabitsRequest(BaseModel):
    """Hábitos deportivos completos (HU006). Los errores de una lista nombran la posición
    recibida (`sports[1].sport`, `availability.days[0]`)."""

    model_config = ConfigDict(extra="forbid")

    expected_version: ExpectedVersion = None
    sports: Annotated[list[SportHabit], Field(min_length=1, max_length=5)]
    availability: SportAvailability
    primary_goal: PrimaryGoal | None = None


class SportHabitsPatchRequest(BaseModel):
    """Edición parcial de los hábitos deportivos (HU008, ADR-035): una o más secciones; cada
    sección enviada reemplaza completa a la vigente y las no enviadas se conservan.
    `primary_goal: null` borra el objetivo. Sin secciones → `422 required` (`field: null`)."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "anyOf": [
                {"required": ["sports"]},
                {"required": ["availability"]},
                {"required": ["primary_goal"]},
            ]
        },
    )

    expected_version: Annotated[
        int, Field(ge=1, description="Obligatoria: la versión vigente de los hábitos.")
    ]
    sports: Annotated[
        list[SportHabit] | None,
        Field(min_length=1, max_length=5, description="Reemplaza la lista completa."),
    ] = None
    availability: Annotated[
        SportAvailability | None, Field(description="Reemplaza días y franjas.")
    ] = None
    primary_goal: Annotated[
        PrimaryGoal | None, Field(description="Enviado como `null`, borra el objetivo.")
    ] = None


class NutritionHabitsRequest(BaseModel):
    """Hábitos alimenticios completos (HU009, ADR-035), con el consentimiento de datos de
    salud vigente. Sin texto libre: solo valores de los catálogos. Los errores de una lista
    nombran la posición recibida (`allergies[1]`)."""

    model_config = ConfigDict(extra="forbid")

    expected_version: ExpectedVersion = None
    consent: HealthConsentRequest
    diet_pattern: DietPattern
    restrictions: Annotated[
        list[Restriction], Field(max_length=7, description="Valores únicos; puede ir vacía.")
    ]
    allergies: Annotated[
        list[Allergy], Field(max_length=9, description="Valores únicos; puede ir vacía.")
    ]
    meals_per_day: Annotated[int, Field(ge=1, le=8)]
    hydration_liters_per_day: Annotated[
        str | None,
        Field(
            pattern=REQUEST_DECIMAL,
            description="Cadena decimal con hasta un decimal, de 0.5 a 10.0 litros.",
            examples=["2.5"],
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


class SportHabitView(BaseModel):
    sport: Sport
    frequency_per_week: int
    session_duration_minutes: int
    intensity: Intensity
    level: Level
    years_practicing: int | None


class SportAvailabilityView(BaseModel):
    days: Annotated[list[Day], Field(description="En el orden del catálogo (`MON`…`SUN`).")]
    time_slots: Annotated[list[TimeSlot], Field(description="En el orden del catálogo.")]


class SportHabitsSection(BaseModel):
    """Hábitos normalizados: deportes, días y franjas en el orden de su catálogo."""

    version: int
    sports: list[SportHabitView]
    availability: SportAvailabilityView
    primary_goal: PrimaryGoal | None
    created_at: Annotated[datetime, Field(description=UTC_INSTANT)]
    updated_at: Annotated[datetime, Field(description=UTC_INSTANT)]


class SportHabitsResponse(SportHabitsSection, _Owner):
    """Respuesta del comando: la sección con el `user_id` del token."""


class NutritionHabitsSection(BaseModel):
    """Hábitos alimenticios normalizados: restricciones y alergias en el orden de su
    catálogo. No prescriben ni diagnostican."""

    version: int
    consent: HealthConsent
    diet_pattern: DietPattern
    restrictions: list[Restriction]
    allergies: list[Allergy]
    meals_per_day: int
    hydration_liters_per_day: Annotated[
        str | None, Field(pattern=RESPONSE_DECIMAL, description="Un decimal.", examples=["2.5"])
    ]
    created_at: Annotated[datetime, Field(description=UTC_INSTANT)]
    updated_at: Annotated[datetime, Field(description=UTC_INSTANT)]


class NutritionHabitsResponse(NutritionHabitsSection, _Owner):
    """Respuesta del comando: la sección con el `user_id` del token."""


class ProfileVersions(BaseModel):
    demographic: int | None
    physiological: int | None
    sport_habits: int | None
    nutrition_habits: int | None


class ProfileResponse(BaseModel):
    """Perfil proyectado (consistencia eventual): `null` en las secciones aún sin datos."""

    user_id: UUID
    demographic: DemographicProfileSection | None
    physiological: PhysiologicalProfileSection | None
    sport_habits: Annotated[
        SportHabitsSection | None, Field(description="HU006; `null` mientras no exista.")
    ]
    nutrition_habits: Annotated[
        NutritionHabitsSection | None, Field(description="HU009; `null` mientras no exista.")
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
_PATCH_ERRORS: dict[int | str, dict[str, Any]] = {
    **_COMMAND_ERRORS,
    HTTPStatus.NOT_FOUND.value: {
        "model": ErrorResponse,
        "description": "`sport_habits_not_found`: primero se registran los hábitos (HU006).",
    },
    HTTPStatus.CONFLICT.value: {
        "model": ErrorResponse,
        "description": '`version_conflict` con `details=[{field: "expected_version", issue: '
        '"<versión vigente>"}]`, o `idempotency_key_reused` (la clave ya se usó con otro cuerpo '
        "o por otro deportista).",
    },
}
# Estados del servicio que se entregan tal cual; cualquier otro es `upstream_unavailable`.
_PUT_STATUSES = frozenset(
    {
        HTTPStatus.OK,
        HTTPStatus.CREATED,
        HTTPStatus.UNAUTHORIZED,
        HTTPStatus.FORBIDDEN,
        HTTPStatus.CONFLICT,
        HTTPStatus.UNPROCESSABLE_ENTITY,
    }
)
# HU008: el `PATCH` solo edita (nunca `201`) y responde `404` si la sección no existe.
_PATCH_STATUSES = (_PUT_STATUSES - {HTTPStatus.CREATED}) | {HTTPStatus.NOT_FOUND}


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
        expected: frozenset[HTTPStatus] = _PUT_STATUSES,
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
                set(expected),
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

    @router.put(
        "/v1/me/sport-habits",
        response_model=SportHabitsResponse,
        summary="Registrar o reemplazar mis hábitos deportivos (HU006, ADR-034)",
        description="Registrar hábitos no crea planes ni recomendaciones.",
        openapi_extra=_request_body(SportHabitsRequest),
        responses={
            HTTPStatus.OK.value: {
                "description": "Sección reemplazada (o reintento de un reemplazo).",
                "headers": _COMMAND_HEADERS_DOC,
            },
            HTTPStatus.CREATED.value: {
                "model": SportHabitsResponse,
                "description": "Sección creada (o reintento de la creación).",
                "headers": _COMMAND_HEADERS_DOC,
            },
            **_COMMAND_ERRORS,
        },
    )
    async def put_sport_habits(
        request: Request,
        idempotency_key: Annotated[
            str | None, Header(alias=IDEMPOTENCY_HEADER, description=_IDEMPOTENCY_DESCRIPTION)
        ] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        return await run_in_threadpool(
            put_section,
            users.put_sport_habits,
            await request.body(),
            idempotency_key,
            authorization,
        )

    @router.patch(
        "/v1/me/sport-habits",
        response_model=SportHabitsResponse,
        summary="Actualizar parcialmente mis hábitos deportivos (HU008, ADR-035)",
        description="Cada sección enviada (`sports`, `availability`, `primary_goal`) reemplaza "
        "completa a la vigente y las demás se conservan; se revalida el agregado completo. No "
        "modifica planes de entrenamiento vigentes.",
        openapi_extra=_request_body(SportHabitsPatchRequest),
        responses={
            HTTPStatus.OK.value: {
                "description": "Secciones actualizadas (o reintento): estado completo con la "
                "versión nueva.",
                "headers": _COMMAND_HEADERS_DOC,
            },
            **_PATCH_ERRORS,
        },
    )
    async def patch_sport_habits(
        request: Request,
        idempotency_key: Annotated[
            str | None, Header(alias=IDEMPOTENCY_HEADER, description=_IDEMPOTENCY_DESCRIPTION)
        ] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        return await run_in_threadpool(
            put_section,
            users.patch_sport_habits,
            await request.body(),
            idempotency_key,
            authorization,
            _PATCH_STATUSES,
        )

    @router.put(
        "/v1/me/nutrition-habits",
        response_model=NutritionHabitsResponse,
        summary="Registrar o reemplazar mis hábitos alimenticios (HU009, ADR-035)",
        description="Exige el consentimiento vigente de datos de salud; sin él no se guarda "
        "nada. Registrar hábitos no crea planes, consumos ni recomendaciones.",
        openapi_extra=_request_body(NutritionHabitsRequest),
        responses={
            HTTPStatus.OK.value: {
                "description": "Sección reemplazada (o reintento de un reemplazo).",
                "headers": _COMMAND_HEADERS_DOC,
            },
            HTTPStatus.CREATED.value: {
                "model": NutritionHabitsResponse,
                "description": "Sección creada (o reintento de la creación).",
                "headers": _COMMAND_HEADERS_DOC,
            },
            **_COMMAND_ERRORS,
        },
    )
    async def put_nutrition_habits(
        request: Request,
        idempotency_key: Annotated[
            str | None, Header(alias=IDEMPOTENCY_HEADER, description=_IDEMPOTENCY_DESCRIPTION)
        ] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        return await run_in_threadpool(
            put_section,
            users.put_nutrition_habits,
            await request.body(),
            idempotency_key,
            authorization,
        )

    @router.get(
        "/v1/me/profile",
        response_model=ProfileResponse,
        summary="Mi perfil de deportista (HU005, HU006 y HU009; ADR-034 y ADR-035)",
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
