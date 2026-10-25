"""Deportes de HU006 que coinciden con la disciplina de un evento (ADR-041 §HU027.2, ADR-045 §8).

`discipline` es texto libre (ADR-028). El worker la normaliza (minúsculas, sin acentos,
recortada y con los espacios internos reducidos a uno) y la busca en la tabla de alias
`discipline-aliases.v1.json`. La tabla es una copia exacta de la que versiona `bff-mobile`
para HU027 (son desplegables distintos y no comparten código de dominio); una prueba de
contrato compara las dos copias. `other` no tiene alias y nunca coincide.
"""

import json
import unicodedata
from importlib.resources import files
from typing import Any

ALIASES_FILE = "discipline-aliases.v1.json"
ALIASES_VERSION = "discipline-aliases.v1"


def normalize(text: str) -> str:
    """`" Media  Maratón "` → `"media maraton"`."""
    decomposed = unicodedata.normalize("NFKD", text)
    plain = "".join(char for char in decomposed if not unicodedata.combining(char))
    return " ".join(plain.casefold().split())


def load_aliases(document: dict[str, Any] | None = None) -> dict[str, frozenset[str]]:
    """Alias por deporte; valida versión, normalización y que ningún alias se repita."""
    if document is None:
        source = files("events_management.domain.notifications").joinpath(ALIASES_FILE)
        document = json.loads(source.read_text(encoding="utf-8"))
    if document.get("version") != ALIASES_VERSION:
        raise ValueError(f"{ALIASES_FILE}: versión inesperada")
    aliases: dict[str, frozenset[str]] = {}
    seen: set[str] = set()
    for sport, names in document["sports"].items():
        if sport == "other":
            raise ValueError(f"{ALIASES_FILE}: `other` no admite alias")
        for name in names:
            if name != normalize(name) or name in seen:
                raise ValueError(f"{ALIASES_FILE}: alias no normalizado o repetido: {name}")
            seen.add(name)
        aliases[sport] = frozenset(names)
    return aliases


def sports_for_discipline(discipline: str, aliases: dict[str, frozenset[str]]) -> list[str]:
    """Deportes (en el orden de la tabla) cuya lista de alias contiene la disciplina."""
    normalized = normalize(discipline)
    return [sport for sport, names in aliases.items() if normalized in names]
