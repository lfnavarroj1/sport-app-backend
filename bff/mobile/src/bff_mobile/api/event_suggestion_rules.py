"""Reglas de las sugerencias de eventos (HU027, ADR-041), sin transporte.

- Normalización de disciplina y ciudad: minúsculas, sin acentos, recortado y con los espacios
  internos reducidos a uno («Natación», « natacion » y «NATACIÓN» son iguales).
- Deporte y disciplina: `discipline-aliases.v1.json` (versionado junto a este módulo) asigna a
  cada deporte del catálogo de HU006 sus disciplinas normalizadas; la disciplina del evento
  coincide si es igual a uno de esos alias. `other` no tiene alias y nunca coincide.
- Solo se sugieren eventos `PUBLISHED` que aún no han empezado (`starts_at > ahora`).
- Personalizada (deportes **y** ciudad conocidos): `SPORT_MATCH` y `CITY_MATCH` primero, luego
  solo `SPORT_MATCH`, luego solo `CITY_MATCH` y al final los demás; dentro de cada grupo,
  `starts_at` y `event_id` ascendentes. Un evento sin coincidencias lleva `UPCOMING`.
- No personalizada: todos por `starts_at` y `event_id`, cada uno con `UPCOMING`.
No verifica cupo (lo decide HU041 al inscribirse).
"""

import json
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from importlib.resources import files
from typing import Any

from bff_mobile.api.service_suggestion_rules import instant

ALIASES_FILE = "discipline-aliases.v1.json"
ALIASES_VERSION = "discipline-aliases.v1"
SPORT_MATCH = "SPORT_MATCH"
CITY_MATCH = "CITY_MATCH"
UPCOMING = "UPCOMING"
PUBLISHED = "PUBLISHED"


def normalize(text: str) -> str:
    """Minúsculas, sin acentos (marcas combinantes de NFKD) y espacios recortados y únicos."""
    decomposed = unicodedata.normalize("NFKD", text)
    plain = "".join(char for char in decomposed if not unicodedata.combining(char))
    return " ".join(plain.casefold().split())


def load_aliases(document: dict[str, Any] | None = None) -> dict[str, frozenset[str]]:
    """Alias por deporte; valida versión, normalización y que ningún alias se repita."""
    if document is None:
        source = files("bff_mobile.api").joinpath(ALIASES_FILE)
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


@dataclass(frozen=True)
class AthleteMatch:
    """Lo que la regla usa del contexto del deportista, ya normalizado."""

    disciplines: frozenset[str]
    city: str


@dataclass(frozen=True)
class Suggestion:
    event: dict[str, Any]
    reasons: tuple[str, ...]


def athlete_match(
    sports: Iterable[str], city: str, aliases: dict[str, frozenset[str]]
) -> AthleteMatch:
    disciplines = frozenset(name for sport in sports for name in aliases.get(sport, frozenset()))
    return AthleteMatch(disciplines=disciplines, city=normalize(city))


def _reasons(event: dict[str, Any], match: AthleteMatch) -> tuple[str, ...]:
    reasons = []
    if normalize(event["discipline"]) in match.disciplines:
        reasons.append(SPORT_MATCH)
    if normalize(event["location"]["city"]) == match.city:
        reasons.append(CITY_MATCH)
    return tuple(reasons) or (UPCOMING,)


_GROUP = {(SPORT_MATCH, CITY_MATCH): 0, (SPORT_MATCH,): 1, (CITY_MATCH,): 2, (UPCOMING,): 3}


def suggest(
    events: Iterable[dict[str, Any]], match: AthleteMatch | None, now: datetime
) -> list[Suggestion]:
    """Eventos sugeridos con sus motivos, en el orden de la respuesta (`match` `None`: no
    personalizada)."""
    upcoming = [
        event
        for event in events
        if event["status"] == PUBLISHED and instant(event["starts_at"]) > now
    ]
    suggestions = [
        Suggestion(event, (UPCOMING,) if match is None else _reasons(event, match))
        for event in upcoming
    ]
    return sorted(
        suggestions,
        key=lambda item: (
            _GROUP[item.reasons],
            instant(item.event["starts_at"]),
            str(item.event["event_id"]),
        ),
    )
