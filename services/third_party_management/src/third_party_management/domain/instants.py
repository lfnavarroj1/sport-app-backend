"""Instantes en UTC con sufijo `Z` (regla del repositorio: el frontend convierte)."""

from datetime import UTC, datetime


def utc_instant(value: datetime) -> str:
    """`2026-10-20T11:00:00Z` (sin microsegundos si son cero)."""
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
