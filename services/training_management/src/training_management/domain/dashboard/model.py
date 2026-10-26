"""Tablero de entrenamientos del deportista (HU033, ADR-043), sin infraestructura.

- Periodo `[from, to)` con instantes con desfase; por defecto los últimos 30 días en UTC;
  como mucho 366 días. Una sesión pertenece al periodo por su `ended_at` (como el historial de
  HU011) y un indicador por su `measured_at`.
- Las semanas son ISO (lunes a domingo) en la zona del `from`: `week_start` es el lunes local.
- Duraciones en minutos enteros redondeados `ROUND_HALF_UP` a partir de los segundos sumados.
- `distance_meters` suma solo las filas que la tienen (las actividades importadas de Strava de
  HU031; las sesiones de HU032 no registran distancia); sin filas con distancia es `None`
  (decisión del 2026-10-24), nunca un valor inventado.
- Sin datos: `has_data = False`, totales en cero y listas vacías (CA2).
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum

from training_management.domain.plans.model import Sport

DEFAULT_PERIOD = timedelta(days=30)
MAX_PERIOD = timedelta(days=366)
_SECONDS_PER_MINUTE = Decimal(60)
# `YYYY-MM-DDTHH:MM:SS[.ffffff](Z|±HH:MM)`: el desfase es obligatorio (CA3).
_INSTANT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?(Z|[+-]\d{2}:\d{2})$")
# En una consulta sin codificar, `+09:00` llega como ` 09:00` (el `+` se decodifica a espacio).
_DECODED_PLUS = re.compile(r"^(.*T\d{2}:\d{2}:\d{2}(\.\d{1,6})?) (\d{2}:\d{2})$")


class Source(StrEnum):
    """Origen de una sesión (ADR-043): `SPORTAPP` (HU032) o `STRAVA` (importada, HU031)."""

    SPORTAPP = "SPORTAPP"
    STRAVA = "STRAVA"


class InvalidDashboardPeriod(Exception):
    """Periodo inválido; `field` es `from` o `to` (CA4: `422 validation_error`)."""

    code = "validation_error"

    def __init__(self, field: str, issue: str) -> None:
        super().__init__(f"{field}: {issue}")
        self.field = field
        self.issue = issue


@dataclass(frozen=True)
class DashboardPeriod:
    start: datetime
    end: datetime

    @property
    def offset(self) -> timedelta:
        """Desfase de la zona del `from`: en él se calculan las semanas ISO."""
        return self.start.utcoffset() or timedelta(0)


def _parse_instant(value: str, field: str) -> datetime:
    text = value.strip()
    decoded = _DECODED_PLUS.match(text)
    if decoded:
        text = f"{decoded.group(1)}+{decoded.group(3)}"
    if not _INSTANT.match(text):
        raise InvalidDashboardPeriod(field, "invalid_datetime")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise InvalidDashboardPeriod(field, "invalid_datetime") from None
    if parsed.tzinfo is None:  # Inalcanzable con la expresión regular; defensa explícita.
        raise InvalidDashboardPeriod(field, "invalid_datetime")
    return parsed


def resolve_period(raw_from: str | None, raw_to: str | None, now: datetime) -> DashboardPeriod:
    """Valida `from`/`to`; sin ninguno, los últimos 30 días en UTC hasta `now`."""
    if raw_from is None and raw_to is None:
        end = now.astimezone(UTC)
        return DashboardPeriod(start=end - DEFAULT_PERIOD, end=end)
    if raw_from is None:
        raise InvalidDashboardPeriod("from", "required")
    if raw_to is None:
        raise InvalidDashboardPeriod("to", "required")
    start = _parse_instant(raw_from, "from")
    end = _parse_instant(raw_to, "to")
    if end <= start:
        raise InvalidDashboardPeriod("to", "ends_before_starts")
    if end - start > MAX_PERIOD:
        raise InvalidDashboardPeriod("to", "out_of_range")
    return DashboardPeriod(start=start, end=end)


def iso_week_start(instant: datetime, offset: timedelta) -> date:
    """Lunes de la semana ISO de `instant` en la zona con ese desfase."""
    local = instant.astimezone(timezone(offset)).date()
    return local - timedelta(days=local.isoweekday() - 1)


def minutes(seconds: int) -> int:
    return int((Decimal(seconds) / _SECONDS_PER_MINUTE).quantize(Decimal(1), ROUND_HALF_UP))


@dataclass(frozen=True)
class SessionBucket:
    """Sesiones del deportista agrupadas por deporte, semana ISO local y fuente."""

    sport: str
    week_start: date
    source: Source
    sessions_count: int
    duration_seconds: int
    # Suma de las filas con distancia y cuántas la tenían (0 → no disponible).
    distance_meters: int = 0
    sessions_with_distance: int = 0


@dataclass(frozen=True)
class LatestMetric:
    """Último resultado de un indicador de ADR-030 dentro del periodo."""

    indicator: str
    value: Decimal
    unit: str
    method_code: str
    method_version: int
    measured_at: datetime
    disclaimer_key: str


@dataclass(frozen=True)
class Aggregate:
    sessions_count: int = 0
    duration_seconds: int = 0
    distance_meters: int = 0
    sessions_with_distance: int = 0

    def plus(self, bucket: SessionBucket) -> "Aggregate":
        return Aggregate(
            sessions_count=self.sessions_count + bucket.sessions_count,
            duration_seconds=self.duration_seconds + bucket.duration_seconds,
            distance_meters=self.distance_meters + bucket.distance_meters,
            sessions_with_distance=self.sessions_with_distance + bucket.sessions_with_distance,
        )

    @property
    def total_minutes(self) -> int:
        return minutes(self.duration_seconds)

    @property
    def average_minutes(self) -> int:
        if self.sessions_count == 0:
            return 0
        average = Decimal(self.duration_seconds) / self.sessions_count / _SECONDS_PER_MINUTE
        return int(average.quantize(Decimal(1), ROUND_HALF_UP))

    @property
    def distance(self) -> int | None:
        return self.distance_meters if self.sessions_with_distance else None


@dataclass(frozen=True)
class TrainingDashboard:
    period: DashboardPeriod
    totals: Aggregate
    by_sport: tuple[tuple[str, Aggregate], ...]
    by_week: tuple[tuple[date, Aggregate], ...]
    by_source: tuple[tuple[Source, Aggregate], ...]
    latest_metrics: tuple[LatestMetric, ...]
    sessions_available: bool = True
    metrics_available: bool = True

    @property
    def has_data(self) -> bool:
        return self.totals.sessions_count > 0 or bool(self.latest_metrics)


_SPORT_ORDER = {sport.value: position for position, sport in enumerate(Sport)}


def build_dashboard(
    period: DashboardPeriod,
    buckets: Iterable[SessionBucket],
    metrics: Iterable[LatestMetric],
    *,
    sessions_available: bool = True,
    metrics_available: bool = True,
) -> TrainingDashboard:
    """Combina los grupos de sesiones y los últimos indicadores en el tablero de ADR-043.

    Deportes en el orden de su catálogo, semanas ascendentes, ambas fuentes siempre presentes
    (en cero si no tienen sesiones) e indicadores por código.
    """
    totals = Aggregate()
    sports: dict[str, Aggregate] = {}
    weeks: dict[date, Aggregate] = {}
    sources: dict[Source, Aggregate] = {source: Aggregate() for source in Source}
    for bucket in buckets:
        if bucket.sessions_count <= 0:
            continue
        totals = totals.plus(bucket)
        sports[bucket.sport] = sports.get(bucket.sport, Aggregate()).plus(bucket)
        weeks[bucket.week_start] = weeks.get(bucket.week_start, Aggregate()).plus(bucket)
        sources[bucket.source] = sources[bucket.source].plus(bucket)
    return TrainingDashboard(
        period=period,
        totals=totals,
        by_sport=tuple(
            sorted(
                sports.items(),
                key=lambda item: (_SPORT_ORDER.get(item[0], len(_SPORT_ORDER)), item[0]),
            )
        ),
        by_week=tuple(sorted(weeks.items())),
        by_source=tuple((source, sources[source]) for source in Source),
        latest_metrics=tuple(sorted(metrics, key=lambda metric: metric.indicator)),
        sessions_available=sessions_available,
        metrics_available=metrics_available,
    )
