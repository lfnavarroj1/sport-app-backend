"""Propagación de `correlation_id` y registro técnico de cada solicitud HTTP."""

import logging
import time
import uuid
from contextvars import ContextVar

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

CORRELATION_HEADER = "X-Correlation-ID"

correlation_id_var: ContextVar[str | None] = ContextVar("correlation_id", default=None)

_logger = logging.getLogger("sportapp.http")


def current_correlation_id() -> str | None:
    return correlation_id_var.get()


def _accepted_correlation_id(raw_value: str | None) -> str:
    """Acepta solo UUID para evitar inyección en logs; si no, genera uno nuevo."""
    if raw_value:
        try:
            return str(uuid.UUID(raw_value))
        except ValueError:
            pass
    return str(uuid.uuid4())


class CorrelationMiddleware:
    """Middleware ASGI: fija el `correlation_id`, lo devuelve y registra el resultado.

    No registra cuerpos, parámetros de consulta ni encabezados.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = dict(scope["headers"]).get(CORRELATION_HEADER.lower().encode("latin-1"))
        correlation_id = _accepted_correlation_id(incoming.decode("latin-1") if incoming else None)
        token = correlation_id_var.set(correlation_id)
        started = time.perf_counter()
        status_code = 500

        async def send_with_header(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                MutableHeaders(scope=message)[CORRELATION_HEADER] = correlation_id
            await send(message)

        try:
            await self.app(scope, receive, send_with_header)
        finally:
            route = scope.get("route")
            _logger.info(
                "request.completed",
                extra={
                    "fields": {
                        "method": scope["method"],
                        "route": getattr(route, "path", "<sin_ruta>"),
                        "status_code": status_code,
                        "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                    }
                },
            )
            correlation_id_var.reset(token)
