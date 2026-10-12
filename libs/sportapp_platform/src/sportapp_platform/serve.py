"""Punto de entrada de los contenedores: `python -m sportapp_platform.serve`.

Variables:
- `APP_FACTORY` (obligatoria): fábrica `modulo:funcion` del componente.
- `HTTP_PORT` (opcional, 8000): puerto de escucha.
"""

import os

import uvicorn

from sportapp_platform.config import ConfigurationError

DEFAULT_PORT = 8000
# Dentro del contenedor se escucha en todas las interfaces; el aislamiento lo da la red del pod.
LISTEN_HOST = "0.0.0.0"  # noqa: S104  # nosec B104


def resolve_server_options(environ: dict[str, str]) -> tuple[str, int]:
    factory = environ.get("APP_FACTORY", "").strip()
    if ":" not in factory:
        raise ConfigurationError("Configuración inválida o incompleta: APP_FACTORY")
    try:
        port = int(environ.get("HTTP_PORT", DEFAULT_PORT))
    except ValueError:
        raise ConfigurationError("Configuración inválida o incompleta: HTTP_PORT") from None
    return factory, port


def main() -> None:
    factory, port = resolve_server_options(dict(os.environ))
    uvicorn.run(
        factory,
        factory=True,
        host=LISTEN_HOST,
        port=port,
        log_config=None,
        access_log=False,
        server_header=False,
    )


if __name__ == "__main__":
    main()
