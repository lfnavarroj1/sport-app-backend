"""Formato único de error HTTP.

Implementa el formato **propuesto** en los lineamientos transversales
(`code`, `message`, `correlation_id`, `details`). Sigue siendo una propuesta
hasta que se publique el contrato compartido; ningún servicio define otro.
"""

import logging
from http import HTTPStatus

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from sportapp_platform.correlation import current_correlation_id

_logger = logging.getLogger("sportapp.errors")

_CODES_BY_STATUS = {
    HTTPStatus.NOT_FOUND: ("not_found", "El recurso solicitado no existe."),
    HTTPStatus.METHOD_NOT_ALLOWED: ("method_not_allowed", "Método no permitido para el recurso."),
}


class ErrorDetail(BaseModel):
    field: str | None = None
    issue: str


class ErrorResponse(BaseModel):
    code: str
    message: str
    correlation_id: str | None
    details: list[ErrorDetail] = Field(default_factory=list)


def error_response(
    status_code: int, code: str, message: str, details: list[ErrorDetail] | None = None
) -> JSONResponse:
    body = ErrorResponse(
        code=code,
        message=message,
        correlation_id=current_correlation_id(),
        details=details or [],
    )
    return JSONResponse(status_code=status_code, content=jsonable_encoder(body))


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(RequestValidationError)
    async def handle_validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        # Solo ubicación y tipo: el valor recibido puede ser sensible y no se devuelve.
        details = [
            ErrorDetail(field=".".join(str(part) for part in error["loc"]), issue=error["type"])
            for error in exc.errors()
        ]
        return error_response(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            "validation_error",
            "La solicitud no es válida.",
            details,
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code, message = _CODES_BY_STATUS.get(
            HTTPStatus(exc.status_code), ("http_error", "No fue posible procesar la solicitud.")
        )
        return error_response(exc.status_code, code, message)

    @app.exception_handler(Exception)
    async def handle_unexpected(_: Request, exc: Exception) -> JSONResponse:
        _logger.error("request.unhandled_error", exc_info=exc)
        return error_response(
            HTTPStatus.INTERNAL_SERVER_ERROR, "internal_error", "Ocurrió un error interno."
        )
