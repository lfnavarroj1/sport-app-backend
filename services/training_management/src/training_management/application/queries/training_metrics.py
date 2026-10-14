"""HU034: catálogo de métodos e historial propio de indicadores (ADR-030).

- Ambas consultas exigen `athlete` con `training.metrics.ftp_vo2max` (Afiliación, sin
  transacción local abierta).
- El catálogo es el inyectado (versionado e inmutable); el `ETag` es su versión.
- El historial lee la proyección `training_metric_result` del deportista del token,
  filtra opcionalmente por `metric_code` del catálogo (desconocido → `unknown_metric`),
  ordena por `measured_at` desc, `calculation_id` desc y pagina por cursor keyset opaco
  (sin pérdidas ni repeticiones aunque lleguen resultados nuevos).
"""

import base64
import binascii
import json
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from training_management.application.access import require_metrics_capability
from training_management.application.ports import (
    Caller,
    CapabilityChecker,
    MetricResultPosition,
    MetricResultQuery,
    MetricResultReader,
)
from training_management.domain.metrics.catalog import MetricCatalog
from training_management.domain.metrics.model import TrainingMetricCalculation

DEFAULT_LIMIT = 20
MAX_LIMIT = 100


class InvalidMetricResultQuery(Exception):
    """Indicador, límite o cursor inválido; se informa el parámetro afectado."""

    code = "validation_error"

    def __init__(self, field: str, issue: str) -> None:
        super().__init__(f"{field}: {issue}")
        self.field = field
        self.issue = issue


@dataclass(frozen=True)
class MetricResultFilters:
    metric_code: str | None = None
    limit: int = DEFAULT_LIMIT
    cursor: str | None = None


@dataclass(frozen=True)
class MetricResultPage:
    items: tuple[TrainingMetricCalculation, ...]
    next_cursor: str | None


def encode_cursor(position: MetricResultPosition) -> str:
    raw = json.dumps(
        {"m": position.measured_at.isoformat(), "c": str(position.calculation_id)},
        separators=(",", ":"),
    )
    return base64.urlsafe_b64encode(raw.encode("utf-8")).rstrip(b"=").decode("ascii")


def decode_cursor(cursor: str) -> MetricResultPosition:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        document = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
        measured_at = datetime.fromisoformat(document["m"])
        calculation_id = UUID(document["c"])
    except (ValueError, KeyError, TypeError, binascii.Error, UnicodeError):
        raise InvalidMetricResultQuery("cursor", "invalid_cursor") from None
    if measured_at.tzinfo is None:
        raise InvalidMetricResultQuery("cursor", "invalid_cursor")
    return MetricResultPosition(measured_at=measured_at, calculation_id=calculation_id)


class GetMetricCatalog:
    def __init__(self, *, capabilities: CapabilityChecker, catalog: MetricCatalog) -> None:
        self._capabilities = capabilities
        self._catalog = catalog

    def __call__(self, caller: Caller) -> MetricCatalog:
        require_metrics_capability(caller, self._capabilities)
        return self._catalog


class ListTrainingMetricResults:
    def __init__(
        self,
        *,
        reader: MetricResultReader,
        capabilities: CapabilityChecker,
        catalog: MetricCatalog,
    ) -> None:
        self._reader = reader
        self._capabilities = capabilities
        self._catalog = catalog

    def __call__(self, caller: Caller, filters: MetricResultFilters) -> MetricResultPage:
        require_metrics_capability(caller, self._capabilities)
        if filters.metric_code is not None and not self._catalog.has_metric(filters.metric_code):
            raise InvalidMetricResultQuery("metric_code", "unknown_metric")
        if not 1 <= filters.limit <= MAX_LIMIT:
            raise InvalidMetricResultQuery("limit", "out_of_range")
        after = decode_cursor(filters.cursor) if filters.cursor else None
        # Se pide uno de más para saber si existe una página siguiente.
        entries = self._reader.list(
            MetricResultQuery(
                athlete_id=caller.user_id,
                metric_code=filters.metric_code,
                after=after,
                limit=filters.limit + 1,
            )
        )
        page = entries[: filters.limit]
        next_cursor = (
            encode_cursor(MetricResultPosition(page[-1].measured_at, page[-1].calculation_id))
            if len(entries) > filters.limit
            else None
        )
        return MetricResultPage(items=tuple(page), next_cursor=next_cursor)
