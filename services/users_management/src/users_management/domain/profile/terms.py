"""Normalización de la ciudad para buscar deportistas (HU039, ADR-045).

Igualdad sin distinguir mayúsculas ni acentos sobre el texto recortado. La misma tabla de
equivalencias la usa la consulta SQL (`lower(translate(btrim(...)))` sobre
`athlete_profile_view`), así que el filtro de la base y esta función dan el mismo resultado
sin columnas ni migraciones nuevas.

Copia de `services/events_management/src/events_management/domain/sport_events/terms.py`
(HU023, ADR-040): Eventos normaliza la ciudad del evento con la misma regla y ningún dominio
importa otro (mismo criterio que `countries.py`); un cambio en uno se replica en el otro.

La tabla se deriva de Unicode: cada letra latina (U+00C0 a U+017F) cuya descomposición es una
letra ASCII más signos diacríticos se reemplaza por esa letra (`Á` → `A`, `ñ` → `n`, `ç` → `c`).
"""

import unicodedata


def _equivalences() -> tuple[str, str]:
    accented, plain = [], []
    for code in range(0x00C0, 0x0180):
        character = chr(code)
        decomposed = unicodedata.normalize("NFD", character)
        base = decomposed[0]
        marks = decomposed[1:]
        if (
            len(decomposed) > 1
            and base.isascii()
            and base.isalpha()
            and all(unicodedata.combining(mark) for mark in marks)
        ):
            accented.append(character)
            plain.append(base)
    return "".join(accented), "".join(plain)


ACCENTED, PLAIN = _equivalences()
_TABLE = str.maketrans(ACCENTED, PLAIN)


def normalize_term(value: str) -> str:
    """`"  Bogotá "` → `"bogota"`; `"MEDELLÍN"` → `"medellin"`."""
    return value.strip().translate(_TABLE).lower()
