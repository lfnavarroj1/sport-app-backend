"""Búsqueda de valores sensibles en los registros de log y auditoría capturados.

Se revisan por subcadena el mensaje y cada campo de `fields`. Solo se omite lo que no puede
contener un dato del perfil por construcción, porque de otro modo las cifras sintéticas chocan
con valores técnicos aleatorios:

- un valor que es exactamente un UUID canónico (identificadores generados);
- `duration_ms` cuando es numérico: lo mide el middleware (`68.56` contiene `68.5`). Si llegara
  como texto, se revisa como cualquier otro campo.
"""

import logging
from collections.abc import Iterable, Mapping
from typing import Any
from uuid import UUID

TIMING_FIELDS = frozenset({"duration_ms"})


def is_canonical_uuid(value: object) -> bool:
    if isinstance(value, UUID):
        return True
    if not isinstance(value, str):
        return False
    try:
        return str(UUID(value)) == value.lower()
    except ValueError:
        return False


def _inspectable(fields: Mapping[str, Any]) -> Iterable[tuple[str, str]]:
    for key, value in fields.items():
        if is_canonical_uuid(value):
            continue
        if key in TIMING_FIELDS and isinstance(value, int | float) and not isinstance(value, bool):
            continue
        yield key, str(value)


def leaks(records: Iterable[logging.LogRecord], sensitive: Iterable[str]) -> list[str]:
    """Devuelve `<logger>.<campo>: <valor sensible>` por cada aparición encontrada."""
    values = tuple(sensitive)
    found: list[str] = []
    for record in records:
        texts = [("message", record.getMessage())]
        fields = getattr(record, "fields", None)
        if isinstance(fields, Mapping):
            texts.extend(_inspectable(fields))
        elif fields is not None:
            texts.append(("fields", str(fields)))
        found.extend(
            f"{record.name}.{key}: {value}"
            for key, text in texts
            for value in values
            if value in text
        )
    return found
