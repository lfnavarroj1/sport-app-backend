"""Verificación estática de la dirección de dependencias entre capas.

- `domain` no importa frameworks, ORM, transporte, nube ni otras capas.
- `application` depende de puertos: no importa `infrastructure`, `api` ni frameworks.
"""

import ast
from pathlib import Path

_TECHNICAL_PACKAGES = (
    "fastapi",
    "starlette",
    "pydantic",
    "pydantic_settings",
    "sqlalchemy",
    "alembic",
    "psycopg",
    "httpx",
    "uvicorn",
    "google",
    "sportapp_platform",
)


def _forbidden_prefixes(package: str, layer: str) -> tuple[str, ...]:
    own_layers = {
        "domain": ("application", "infrastructure", "api", "workers"),
        "application": ("infrastructure", "api", "workers"),
    }[layer]
    return _TECHNICAL_PACKAGES + tuple(f"{package}.{name}" for name in own_layers)


def _imported_modules(source: Path) -> list[str]:
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            modules.append(node.module)
    return modules


def find_imports_of(package_dir: Path, forbidden: tuple[str, ...], subdir: str = ".") -> list[str]:
    """Devuelve `archivo -> módulo` por cada importación de un prefijo prohibido."""
    violations: list[str] = []
    for source in sorted((package_dir / subdir).rglob("*.py")):
        for module in _imported_modules(source):
            if any(module == prefix or module.startswith(prefix + ".") for prefix in forbidden):
                violations.append(f"{source.relative_to(package_dir)} -> {module}")
    return violations


def find_layer_violations(package_dir: Path) -> list[str]:
    """Aplica las reglas de dependencia de `domain` y `application`."""
    package = package_dir.name
    violations: list[str] = []
    for layer in ("domain", "application"):
        violations.extend(
            find_imports_of(package_dir, _forbidden_prefixes(package, layer), subdir=layer)
        )
    return violations
