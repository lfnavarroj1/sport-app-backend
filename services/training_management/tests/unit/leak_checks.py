"""Búsqueda de valores sensibles en los campos de un registro de auditoría.

Cada campo se revisa por subcadena salvo el que es exactamente un UUID canónico: los
identificadores se generan al azar y pueden contener las cifras sintéticas (`187`, `150`) sin
que haya filtración. Un UUID acompañado de cualquier otro texto se revisa.
"""

from collections.abc import Iterable, Mapping
from typing import Any
from uuid import UUID


def is_canonical_uuid(value: object) -> bool:
    if isinstance(value, UUID):
        return True
    if not isinstance(value, str):
        return False
    try:
        return str(UUID(value)) == value.lower()
    except ValueError:
        return False


def leaks(fields: Mapping[str, Any], sensitive: Iterable[str]) -> list[str]:
    """Devuelve `<campo>: <valor sensible>` por cada aparición encontrada."""
    values = tuple(sensitive)
    return [
        f"{key}: {value}"
        for key, field in fields.items()
        if not is_canonical_uuid(field)
        for value in values
        if value in str(field)
    ]
