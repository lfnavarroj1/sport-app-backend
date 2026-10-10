"""Logs estructurados en JSON con servicio, ambiente, versión y correlación."""

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

from sportapp_platform.correlation import current_correlation_id


class JsonFormatter(logging.Formatter):
    """Una línea JSON por registro.

    Solo se incluyen los campos explícitos de `extra={"fields": {...}}`; nunca se
    vuelcan cuerpos de solicitudes ni objetos completos.
    """

    def __init__(self, *, service: str, environment: str, version: str) -> None:
        super().__init__()
        self._static = {"service": service, "environment": environment, "version": version}

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            **self._static,
            "correlation_id": current_correlation_id(),
        }
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            payload.update(fields)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(*, service: str, environment: str, version: str, level: str) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(service=service, environment=environment, version=version))
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
    for name in ("uvicorn", "uvicorn.error"):
        logging.getLogger(name).handlers = []
        logging.getLogger(name).propagate = True
    logging.getLogger("uvicorn.access").disabled = True
    # httpx registra URLs completas en INFO; podrían contener parámetros sensibles.
    logging.getLogger("httpx").setLevel(logging.WARNING)
