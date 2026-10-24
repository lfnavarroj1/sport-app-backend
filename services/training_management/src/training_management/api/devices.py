"""HU037 (ADR-044): dispositivos de medición del deportista.

- `POST /v1/me/devices`: vincula un monitor de frecuencia cardiaca BLE (`201` + `Location`); el
  mismo vínculo activo con otra clave se devuelve con `200`.
- `GET /v1/me/devices`: dispositivos propios, activos y revocados.
- `DELETE /v1/me/devices/{device_id}`: revoca (`200`, idempotente); la ingestión de ese
  dispositivo responde desde entonces `409 device_revoked`.

Solo `athlete` con `devices.measurement.connect`. El deportista sale del token. Los cuerpos se
leen como JSON sin validación automática para responder el catálogo de errores
(`details[{field, issue}]`); su esquema se documenta en el OpenAPI. `device_id` es el SHA-256
que calcula el móvil: la dirección BLE nunca llega al backend.
"""

from datetime import datetime
from http import HTTPStatus
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Body, Header, Response
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from sportapp_platform.errors import ErrorDetail, ErrorResponse, error_response
from training_management.api.identity import (
    AUTH_RESPONSES,
    TokenVerifier,
    authenticate,
    forbidden,
)
from training_management.application.access import DEVICES_CAPABILITY
from training_management.application.commands.devices import (
    DeviceResult,
    RegisterDevice,
    RegisterDeviceCommand,
    RevokeDevice,
)
from training_management.application.device_documents import device_document
from training_management.application.errors import (
    CapabilitiesTimeout,
    CapabilitiesUnavailable,
    CapabilityRequired,
    IdempotencyKeyReused,
    PermissionRequired,
)
from training_management.application.queries.devices import ListMyDevices
from training_management.domain.devices.model import (
    DISPLAY_NAME_MAX,
    DISPLAY_NAME_MIN,
    AthleteDevice,
    DeviceAlreadyRegistered,
    DeviceInput,
    DeviceNotFound,
    InvalidDevice,
)
from training_management.domain.sessions.validation import Issue, Violation

IDEMPOTENCY_HEADER = "Idempotency-Key"
REPLAYED_HEADER = "Idempotent-Replayed"
DEVICE_FIELDS = ("device_id", "display_name", "device_type", "protocol")
DEVICE_ID_DESCRIPTION = (
    "SHA-256 en 64 hexadecimales en minúsculas de la dirección BLE y el `user_id`, calculado "
    "por el móvil; la dirección nunca se envía."
)

DeviceId = Annotated[
    str, StringConstraints(pattern=r"^[0-9a-f]{64}$"), Field(description=DEVICE_ID_DESCRIPTION)
]


class RegisterDeviceRequest(BaseModel):
    """Esquema documentado de la vinculación; la validación la hacen el transporte y el
    dominio."""

    model_config = ConfigDict(extra="forbid")

    device_id: DeviceId
    display_name: Annotated[
        str,
        StringConstraints(min_length=DISPLAY_NAME_MIN, max_length=DISPLAY_NAME_MAX),
        Field(description="Nombre visible; se recorta."),
    ]
    device_type: Literal["HEART_RATE_MONITOR"]
    protocol: Literal["BLE_HRS"]


class DeviceResponse(BaseModel):
    """Vínculo del dispositivo; `revoked_at` es `null` mientras está activo."""

    device_id: DeviceId
    athlete_id: UUID
    display_name: str
    device_type: Literal["HEART_RATE_MONITOR"]
    protocol: Literal["BLE_HRS"]
    status: Literal["ACTIVE", "REVOKED"]
    version: int
    created_at: datetime
    updated_at: datetime
    revoked_at: datetime | None

    @classmethod
    def from_device(cls, device: AthleteDevice) -> "DeviceResponse":
        return cls.model_validate(device_document(device))


class DevicesResponse(BaseModel):
    items: list[DeviceResponse] = Field(
        description="Activos y revocados, `created_at` y `device_id` descendentes."
    )


def _schema(model: type[BaseModel]) -> dict[str, Any]:
    schema = model.model_json_schema(ref_template="#/components/schemas/{model}")
    schema.pop("$defs", None)
    return {"requestBody": {"required": True, "content": {"application/json": {"schema": schema}}}}


def _string(document: dict[str, Any], name: str, violations: list[Violation]) -> str | None:
    value = document.get(name)
    if value is None:
        violations.append(Violation(name, Issue.REQUIRED))
        return None
    if not isinstance(value, str):
        violations.append(Violation(name, Issue.UNKNOWN_VALUE))
        return None
    return value


def parse_device(body: object) -> DeviceInput:
    """Forma del cuerpo: campos permitidos, obligatorios y tipos JSON."""
    violations: list[Violation] = []
    if not isinstance(body, dict):
        issue = Issue.REQUIRED if body is None else Issue.UNKNOWN_VALUE
        return DeviceInput(None, None, None, None, (Violation(None, issue),))
    violations.extend(
        Violation(name, Issue.FIELD_NOT_ALLOWED) for name in body if name not in DEVICE_FIELDS
    )
    return DeviceInput(
        device_id=_string(body, "device_id", violations),
        display_name=_string(body, "display_name", violations),
        device_type=_string(body, "device_type", violations),
        protocol=_string(body, "protocol", violations),
        transport_violations=tuple(violations),
    )


def _invalid(
    violations: list[tuple[str | None, str]] | tuple[tuple[str | None, str], ...],
) -> Response:
    return error_response(
        HTTPStatus.UNPROCESSABLE_ENTITY,
        InvalidDevice.code,
        "La solicitud no es válida.",
        [ErrorDetail(field=field, issue=issue) for field, issue in violations],
    )


def _idempotency_key(raw: str | None) -> tuple[UUID | None, tuple[str | None, str] | None]:
    if raw is None or not raw.strip():
        return None, (IDEMPOTENCY_HEADER, Issue.REQUIRED.value)
    try:
        return UUID(raw.strip()), None
    except ValueError:
        return None, (IDEMPOTENCY_HEADER, Issue.INVALID_UUID.value)


_MESSAGES: dict[type[Exception], tuple[HTTPStatus, str]] = {
    DeviceNotFound: (HTTPStatus.NOT_FOUND, "El dispositivo no existe."),
    DeviceAlreadyRegistered: (
        HTTPStatus.CONFLICT,
        "El dispositivo ya está vinculado con otro nombre.",
    ),
    IdempotencyKeyReused: (
        HTTPStatus.CONFLICT,
        "La clave de idempotencia ya se usó con otra solicitud.",
    ),
    CapabilitiesTimeout: (
        HTTPStatus.GATEWAY_TIMEOUT,
        "No fue posible verificar su plan a tiempo. Intente de nuevo.",
    ),
    CapabilitiesUnavailable: (
        HTTPStatus.SERVICE_UNAVAILABLE,
        "No fue posible verificar su plan. Intente de nuevo.",
    ),
}
_HANDLED: tuple[type[Exception], ...] = (
    PermissionRequired,
    CapabilityRequired,
    InvalidDevice,
    *_MESSAGES,
)


def _failure(error: Exception) -> Response:
    if isinstance(error, PermissionRequired | CapabilityRequired):
        return forbidden(error)
    if isinstance(error, InvalidDevice):
        return _invalid(error.violations)
    status, message = _MESSAGES[type(error)]
    return error_response(status, getattr(error, "code", ""), message)


_FORBIDDEN = {
    "model": ErrorResponse,
    "description": "`permission_required` (no es deportista) o `capability_required` "
    f"(`{DEVICES_CAPABILITY}`).",
}
_UPSTREAM: dict[int | str, dict[str, Any]] = {
    HTTPStatus.SERVICE_UNAVAILABLE.value: {
        "model": ErrorResponse,
        "description": "`upstream_unavailable`: no se pudo verificar el plan.",
    },
    HTTPStatus.GATEWAY_TIMEOUT.value: {
        "model": ErrorResponse,
        "description": "`upstream_timeout`: Afiliación no respondió a tiempo.",
    },
}


def build_devices_router(
    *,
    verify_token: TokenVerifier,
    register: RegisterDevice,
    revoke: RevokeDevice,
    list_devices: ListMyDevices,
) -> APIRouter:
    router = APIRouter(prefix="/v1/me/devices", tags=["devices"])

    @router.post(
        "",
        status_code=HTTPStatus.CREATED,
        response_model=DeviceResponse,
        summary="Vincular un monitor de frecuencia cardiaca (HU037)",
        description="Monitor BLE con *Heart Rate Service* (`HEART_RATE_MONITOR`/`BLE_HRS`). "
        "Nace `ACTIVE` en versión 1; un dispositivo revocado vuelve a `ACTIVE` con la versión "
        "siguiente. Reintentar con la misma `Idempotency-Key` (24 horas) devuelve el resultado "
        "original con `Idempotent-Replayed: true`.",
        openapi_extra=_schema(RegisterDeviceRequest),
        responses={
            **AUTH_RESPONSES,
            HTTPStatus.CREATED.value: {
                "headers": {
                    "Location": {
                        "description": "`/v1/me/devices/{device_id}`.",
                        "schema": {"type": "string"},
                    },
                    REPLAYED_HEADER: {
                        "description": "`true` cuando se devuelve el resultado de un reintento.",
                        "schema": {"type": "string"},
                    },
                }
            },
            HTTPStatus.OK.value: {
                "model": DeviceResponse,
                "description": "El mismo dispositivo ya estaba activo con la misma vinculación: "
                "se devuelve sin cambios.",
            },
            HTTPStatus.FORBIDDEN.value: _FORBIDDEN,
            HTTPStatus.CONFLICT.value: {
                "model": ErrorResponse,
                "description": "`device_already_registered` (mismo `device_id` activo con otro "
                "nombre) o `idempotency_key_reused`.",
            },
            HTTPStatus.UNPROCESSABLE_ENTITY.value: {
                "model": ErrorResponse,
                "description": "`validation_error` con `details[{field, issue}]`: `required`, "
                "`field_not_allowed`, `unknown_value`, `invalid_format` (`device_id`), "
                "`invalid_length` (`display_name`) e `invalid_uuid` (`Idempotency-Key`).",
            },
            **_UPSTREAM,
        },
    )
    def register_device(
        response: Response,
        body: Annotated[Any, Body()] = None,
        idempotency_key: Annotated[str | None, Header(alias=IDEMPOTENCY_HEADER)] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response | DeviceResponse:
        caller = authenticate(verify_token, authorization)
        if isinstance(caller, Response):
            return caller
        raw = parse_device(body)
        key, key_problem = _idempotency_key(idempotency_key)
        try:
            if key is None:
                # La clave es inválida: se informan también las reglas de la vinculación.
                problems = [key_problem] if key_problem else []
                try:
                    register.validate(caller, raw)
                except InvalidDevice as error:
                    problems.extend(error.violations)
                return _invalid(problems)
            result = register(
                caller,
                RegisterDeviceCommand(device=raw, idempotency_key=key),
            )
        except _HANDLED as error:
            return _failure(error)
        return _device_response(response, result)

    @router.get(
        "",
        response_model=DevicesResponse,
        summary="Mis dispositivos de medición (HU037)",
        responses={**AUTH_RESPONSES, HTTPStatus.FORBIDDEN.value: _FORBIDDEN, **_UPSTREAM},
    )
    def my_devices(
        response: Response,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response | DevicesResponse:
        caller = authenticate(verify_token, authorization)
        if isinstance(caller, Response):
            return caller
        try:
            devices = list_devices(caller)
        except _HANDLED as error:
            return _failure(error)
        response.headers["Cache-Control"] = "no-store"
        return DevicesResponse(items=[DeviceResponse.from_device(item) for item in devices])

    @router.delete(
        "/{device_id}",
        response_model=DeviceResponse,
        summary="Revocar un dispositivo propio (HU037)",
        description="`ACTIVE` → `REVOKED` con la versión siguiente; uno ya revocado se devuelve "
        "tal cual. Desde ese momento la ingestión de sus mediciones responde "
        "`409 device_revoked`.",
        responses={
            **AUTH_RESPONSES,
            HTTPStatus.FORBIDDEN.value: _FORBIDDEN,
            HTTPStatus.NOT_FOUND.value: {
                "model": ErrorResponse,
                "description": "`device_not_found`: no existe o es de otro deportista.",
            },
            HTTPStatus.UNPROCESSABLE_ENTITY.value: {
                "model": ErrorResponse,
                "description": "`validation_error`: `device_id` `invalid_format`.",
            },
            **_UPSTREAM,
        },
    )
    def revoke_device(
        device_id: str,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response | DeviceResponse:
        caller = authenticate(verify_token, authorization)
        if isinstance(caller, Response):
            return caller
        try:
            result = revoke(caller, device_id)
        except _HANDLED as error:
            return _failure(error)
        return DeviceResponse.from_device(result.device)

    return router


def _device_response(response: Response, result: DeviceResult) -> DeviceResponse:
    if not result.changed:
        # El vínculo ya estaba activo con la misma vinculación: nada se creó.
        response.status_code = HTTPStatus.OK
    else:
        response.headers["Location"] = f"/v1/me/devices/{result.device.device_id}"
    if result.replayed:
        response.headers[REPLAYED_HEADER] = "true"
    return DeviceResponse.from_device(result.device)
