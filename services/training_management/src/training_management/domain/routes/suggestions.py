"""Reglas deterministas de sugerencia de rutas (ADR-041, HU026 CA3 y CA4).

- Solo rutas `PUBLISHED`: una retirada nunca se sugiere.
- Personalizada (el deportista registró deportes en HU006): rutas cuyo `sport` está entre
  sus deportes; primero las de su ciudad, luego por `distance_km` ascendente y por
  `route_id`. Cada una lleva `SPORT_MATCH` y, si coincide la ciudad, `CITY_MATCH`.
- No personalizada (sin deportes, o sin contexto porque Usuarios no respondió): las últimas
  publicadas (`published_at` desc, `route_id` desc), como mucho 20 aunque `limit` sea
  mayor, sin motivos.

La ciudad coincide si, recortada y sin distinguir mayúsculas ni acentos, es la misma, y el
país también lo es cuando se conoce. Nada se persiste: la sugerencia se calcula en cada
consulta sobre `route_view`.
"""

import unicodedata
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from training_management.domain.plans.model import Sport

NON_PERSONALIZED_MAX = 20


class SuggestionReason(StrEnum):
    SPORT_MATCH = "SPORT_MATCH"
    CITY_MATCH = "CITY_MATCH"


def city_key(value: str) -> str:
    """`"  San  José "` → `"san jose"`: comparación sin mayúsculas, acentos ni espacios extra."""
    decomposed = unicodedata.normalize("NFKD", " ".join(value.split()))
    plain = "".join(character for character in decomposed if not unicodedata.combining(character))
    return plain.casefold()


@dataclass(frozen=True)
class SuggestionContext:
    """Contexto del deportista que `bff-mobile` obtiene de Usuarios y pasa como filtros."""

    sports: tuple[Sport, ...]
    city: str | None = None
    country_code: str | None = None

    @property
    def personalized(self) -> bool:
        return bool(self.sports)

    @property
    def city_key(self) -> str | None:
        return city_key(self.city) if self.city else None

    def city_matches(self, city: str, country_code: str) -> bool:
        if self.city_key is None or city_key(city) != self.city_key:
            return False
        return self.country_code is None or self.country_code == country_code


def reasons_for(
    context: SuggestionContext, *, city: str, country_code: str
) -> tuple[SuggestionReason, ...]:
    if not context.personalized:
        return ()
    if context.city_matches(city, country_code):
        return (SuggestionReason.SPORT_MATCH, SuggestionReason.CITY_MATCH)
    return (SuggestionReason.SPORT_MATCH,)


def personalized_order(
    context: SuggestionContext,
    *,
    city: str,
    country_code: str,
    distance_km: Decimal,
    route_id: UUID,
) -> tuple[int, Decimal, UUID]:
    """Clave de orden personalizada: ciudad coincidente, distancia ascendente, `route_id`."""
    return (0 if context.city_matches(city, country_code) else 1, distance_km, route_id)


def suggestion_limit(context: SuggestionContext, limit: int) -> int:
    return limit if context.personalized else min(limit, NON_PERSONALIZED_MAX)
