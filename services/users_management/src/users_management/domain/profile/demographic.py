"""Sección demográfica del perfil (HU005, ADR-034).

MVP: fecha de nacimiento, género, país y ciudad; estado civil, ocupación y rango de
ingresos opcionales. Raza/etnia y religión quedan excluidas (datos de categoría especial
sin finalidad definida).
"""

import re
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import Any

from users_management.domain.profile.countries import COUNTRY_CODES
from users_management.domain.profile.validation import (
    Collector,
    InvalidProfileSection,
    Issue,
    Violation,
)

MIN_AGE_YEARS = 13
MAX_AGE_YEARS = 120
TEXT_MIN = 2
TEXT_MAX = 80
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class Gender(StrEnum):
    FEMALE = "FEMALE"
    MALE = "MALE"
    NON_BINARY = "NON_BINARY"
    PREFER_NOT_TO_SAY = "PREFER_NOT_TO_SAY"


class MaritalStatus(StrEnum):
    SINGLE = "SINGLE"
    MARRIED = "MARRIED"
    DOMESTIC_PARTNERSHIP = "DOMESTIC_PARTNERSHIP"
    DIVORCED = "DIVORCED"
    WIDOWED = "WIDOWED"
    PREFER_NOT_TO_SAY = "PREFER_NOT_TO_SAY"


class IncomeRange(StrEnum):
    LT_1_SMMLV = "LT_1_SMMLV"
    FROM_1_TO_2_SMMLV = "1_2_SMMLV"
    FROM_2_TO_4_SMMLV = "2_4_SMMLV"
    GT_4_SMMLV = "GT_4_SMMLV"
    PREFER_NOT_TO_SAY = "PREFER_NOT_TO_SAY"


@dataclass(frozen=True)
class DemographicInput:
    """Valores con la forma JSON correcta; lo demás ya está en `transport_violations`."""

    expected_version: int | None = None
    birth_date: str | None = None
    gender: str | None = None
    country_code: str | None = None
    city: str | None = None
    marital_status: str | None = None
    occupation: str | None = None
    income_range: str | None = None
    transport_violations: tuple[Violation, ...] = ()

    def __repr__(self) -> str:
        # Datos personales: nunca en trazas ni logs.
        return f"DemographicInput(expected_version={self.expected_version})"


@dataclass(frozen=True)
class DemographicSection:
    expected_version: int | None
    birth_date: date
    gender: Gender
    country_code: str
    city: str
    marital_status: MaritalStatus | None
    occupation: str | None
    income_range: IncomeRange | None

    def __repr__(self) -> str:
        return f"DemographicSection(expected_version={self.expected_version})"

    def fields(self, now: datetime) -> dict[str, Any]:
        # Los campos demográficos no dependen del instante del comando.
        return self._values()

    def normalized(self) -> dict[str, Any]:
        return {"expected_version": self.expected_version, **self._values()}

    def _values(self) -> dict[str, Any]:
        return {
            "birth_date": self.birth_date.isoformat(),
            "gender": self.gender.value,
            "country_code": self.country_code,
            "city": self.city,
            "marital_status": self.marital_status.value if self.marital_status else None,
            "occupation": self.occupation,
            "income_range": self.income_range.value if self.income_range else None,
        }


def age_on(birth_date: date, today: date) -> int:
    """Años cumplidos en `today` (un nacido el 29 de febrero los cumple el 1 de marzo)."""
    before_birthday = (today.month, today.day) < (birth_date.month, birth_date.day)
    return today.year - birth_date.year - int(before_birthday)


def _birth_date(collector: Collector, value: str | None, today: date) -> date | None:
    if value is None:
        return None
    try:
        if not _ISO_DATE.match(value):
            raise ValueError
        parsed = date.fromisoformat(value)
    except ValueError:
        collector.add("birth_date", Issue.INVALID_DATE)
        return None
    if not MIN_AGE_YEARS <= age_on(parsed, today) <= MAX_AGE_YEARS:
        collector.add("birth_date", Issue.OUT_OF_RANGE)
        return None
    return parsed


def _country(collector: Collector, value: str | None) -> str | None:
    if value is None:
        return None
    if value not in COUNTRY_CODES:
        collector.add("country_code", Issue.UNKNOWN_VALUE)
        return None
    return value


def validate_demographic(section: DemographicInput, today: date) -> DemographicSection:
    """Valida todo o nada; `today` es la fecha UTC del comando."""
    collector = Collector(section.transport_violations)
    expected_version = collector.expected_version(section.expected_version)
    birth_date = gender = country_code = city = None
    if collector.required(section.birth_date, "birth_date"):
        birth_date = _birth_date(collector, section.birth_date, today)
    if collector.required(section.gender, "gender"):
        gender = collector.catalog(section.gender, "gender", Gender)
    if collector.required(section.country_code, "country_code"):
        country_code = _country(collector, section.country_code)
    if collector.required(section.city, "city"):
        city = collector.text(section.city, "city", TEXT_MIN, TEXT_MAX)
    marital_status = collector.catalog(section.marital_status, "marital_status", MaritalStatus)
    occupation = collector.text(section.occupation, "occupation", TEXT_MIN, TEXT_MAX)
    income_range = collector.catalog(section.income_range, "income_range", IncomeRange)
    collector.raise_if_any()
    if birth_date is None or gender is None or country_code is None or city is None:
        raise InvalidProfileSection([])  # Inalcanzable: la falta ya se informó arriba.
    return DemographicSection(
        expected_version=expected_version,
        birth_date=birth_date,
        gender=gender,
        country_code=country_code,
        city=city,
        marital_status=marital_status,
        occupation=occupation,
        income_range=income_range,
    )
