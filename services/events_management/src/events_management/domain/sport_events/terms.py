"""Normalización de disciplina y ciudad para filtrar y comparar eventos (HU023, ADR-040).

Igualdad sin distinguir mayúsculas ni acentos sobre el texto recortado. La misma tabla de
equivalencias la usa la consulta SQL (`translate` + `lower` sobre `event_read`), así que el
filtro de la base y esta función dan el mismo resultado sin columnas ni migraciones nuevas.

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
    """`"  Bogotá "` → `"bogota"`; `"RUNNING"` → `"running"`."""
    return value.strip().translate(_TABLE).lower()
