"""Contratos OpenAPI versionados en `contracts/openapi/<componente>.openapi.json`."""

import json
from pathlib import Path

from fastapi import FastAPI


def repository_root(start: Path) -> Path:
    for candidate in (start, *start.parents):
        if (candidate / "contracts").is_dir() and (candidate / "uv.lock").is_file():
            return candidate
    raise FileNotFoundError("No se encontró la raíz del repositorio backend.")


def openapi_contract_path(root: Path, component: str) -> Path:
    return root / "contracts" / "openapi" / f"{component}.openapi.json"


def render_openapi(app: FastAPI) -> str:
    """Serialización determinista para comparar contra el contrato versionado."""
    return json.dumps(app.openapi(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
