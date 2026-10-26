"""Lectura de los cuerpos JSON de HU032 con los errores de ADR-029.

Esta capa solo verifica la forma: campos permitidos (`field_not_allowed`, incluidos
`athlete_id`, `status`, `version`, `duration_seconds` y marcas técnicas), obligatorios
(`required`) y tipos JSON (`unknown_value`). Formatos, ventanas de tiempo y catálogos
son del dominio.
"""

from typing import Any

from training_management.domain.sessions.model import StartInput
from training_management.domain.sessions.validation import Issue, Violation

START_FIELDS = ("session_id", "plan_id", "sport", "started_at")
# HU036 (ADR-044 §10): `reason` opcional (`RISK_STOP`); el catálogo lo valida el dominio.
FINISH_FIELDS = ("ended_at", "reason")


def _unexpected(document: dict[str, Any], allowed: tuple[str, ...]) -> list[Violation]:
    return [Violation(name, Issue.FIELD_NOT_ALLOWED) for name in document if name not in allowed]


def _string(
    document: dict[str, Any], name: str, violations: list[Violation], *, optional: bool = False
) -> str | None:
    value = document.get(name)
    if value is None:
        if not optional:
            violations.append(Violation(name, Issue.REQUIRED))
        return None
    if not isinstance(value, str):
        violations.append(Violation(name, Issue.UNKNOWN_VALUE))
        return None
    return value


def _document(body: object, violations: list[Violation]) -> dict[str, Any] | None:
    if isinstance(body, dict):
        return body
    violations.append(Violation(None, Issue.REQUIRED if body is None else Issue.UNKNOWN_VALUE))
    return None


def parse_start(body: object) -> StartInput:
    violations: list[Violation] = []
    document = _document(body, violations)
    if document is None:
        return StartInput(None, None, None, None, tuple(violations))
    violations.extend(_unexpected(document, START_FIELDS))
    return StartInput(
        session_id=_string(document, "session_id", violations),
        plan_id=_string(document, "plan_id", violations, optional=True),
        sport=_string(document, "sport", violations),
        started_at=_string(document, "started_at", violations),
        transport_violations=tuple(violations),
    )


def parse_finish(body: object) -> tuple[str | None, str | None, tuple[Violation, ...]]:
    """`ended_at`, `reason` (opcional) y las violaciones de forma del cuerpo."""
    violations: list[Violation] = []
    document = _document(body, violations)
    if document is None:
        return None, None, tuple(violations)
    violations.extend(_unexpected(document, FINISH_FIELDS))
    ended_at = _string(document, "ended_at", violations)
    reason = _string(document, "reason", violations, optional=True)
    return ended_at, reason, tuple(violations)
