"""Catálogo versionado e inmutable de métodos de indicadores (ADR-030, HA014).

- Cada método es una versión publicada cuya semántica no cambia: una corrección crea otra
  versión y conserva la anterior para reproducir resultados.
- Un método se compone solo de entradas numéricas escalares (`INTEGER`) y una salida
  numérica con escala fija. Agregar un indicador compatible es añadir un
  `MethodDefinition` a un `MetricCatalog`; no existe CRUD de fórmulas en ejecución.
- El cálculo usa `Decimal` y `ROUND_HALF_UP`; nunca `float` ni el valor del cliente.
- El catálogo se inyecta en los casos de uso: las pruebas de extensibilidad construyen
  otro catálogo sin modificar el del producto.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal, localcontext

INTEGER = "INTEGER"
INPUT_SCALE = 0
# Escala máxima del resultado: la admiten las columnas `NUMERIC(14, 4)` de escritura y lectura.
MAX_RESULT_SCALE = 4
DISCLAIMER_KEY = "estimated_not_diagnostic"
CALCULATION_PRECISION = 28

Formula = Callable[[Mapping[str, Decimal]], Decimal]


@dataclass(frozen=True)
class InputDefinition:
    """Entrada escalar de un método: entero entre `minimum` y `maximum` en `unit`."""

    code: str
    label_key: str
    unit: str
    minimum: Decimal
    maximum: Decimal
    scale: int = INPUT_SCALE
    required: bool = True
    value_type: str = INTEGER


@dataclass(frozen=True)
class MethodDefinition:
    metric_code: str
    label_key: str
    result_unit: str
    result_scale: int
    method_code: str
    method_version: int
    method_label_key: str
    inputs: tuple[InputDefinition, ...]
    formula: Formula = field(compare=False, repr=False)
    disclaimer_key: str = DISCLAIMER_KEY

    def input(self, code: str) -> InputDefinition | None:
        return next((item for item in self.inputs if item.code == code), None)

    def calculate(self, values: Mapping[str, Decimal]) -> Decimal:
        """Resultado redondeado a `result_scale` decimales con `ROUND_HALF_UP`."""
        with localcontext() as context:
            context.prec = CALCULATION_PRECISION
            raw = self.formula(values)
            return raw.quantize(quantum(self.result_scale), rounding=ROUND_HALF_UP)


def quantum(scale: int) -> Decimal:
    """`1` para escala 0, `0.1` para escala 1…"""
    return Decimal(1).scaleb(-scale)


@dataclass(frozen=True)
class MetricCatalog:
    catalog_version: str
    methods: tuple[MethodDefinition, ...]

    def __post_init__(self) -> None:
        identities = [(item.method_code, item.method_version) for item in self.methods]
        if len(set(identities)) != len(identities):
            raise ValueError("El catálogo repite un método y versión.")
        for method in self.methods:
            codes = [item.code for item in method.inputs]
            if not codes or len(set(codes)) != len(codes):
                raise ValueError("Cada método necesita entradas con códigos únicos.")
            if any(
                item.value_type != INTEGER or item.scale != INPUT_SCALE for item in method.inputs
            ):
                raise ValueError("Solo se admiten entradas enteras escalares.")
            if not 0 <= method.result_scale <= MAX_RESULT_SCALE:
                raise ValueError("La escala del resultado está fuera de lo admitido.")

    @property
    def etag(self) -> str:
        return f'"{self.catalog_version}"'

    def has_metric(self, metric_code: str) -> bool:
        return any(item.metric_code == metric_code for item in self.methods)

    def has_method(self, metric_code: str, method_code: str) -> bool:
        return any(
            item.metric_code == metric_code and item.method_code == method_code
            for item in self.methods
        )

    def find(
        self, metric_code: str, method_code: str, method_version: int
    ) -> MethodDefinition | None:
        return next(
            (
                item
                for item in self.methods
                if item.metric_code == metric_code
                and item.method_code == method_code
                and item.method_version == method_version
            ),
            None,
        )


def _ftp_20_min_95(values: Mapping[str, Decimal]) -> Decimal:
    return values["mean_power_watts"] * Decimal("0.95")


def _cooper_12_min(values: Mapping[str, Decimal]) -> Decimal:
    return (values["distance_meters"] - Decimal("504.9")) / Decimal("44.73")


FTP_20_MIN_95_V1 = MethodDefinition(
    metric_code="FTP_ESTIMATED",
    label_key="training_metric.ftp_estimated",
    result_unit="W",
    result_scale=0,
    method_code="FTP_20_MIN_95_V1",
    method_version=1,
    method_label_key="training_metric.method.ftp_20_min_95_v1",
    inputs=(
        InputDefinition(
            code="mean_power_watts",
            label_key="training_metric.input.mean_power_watts",
            unit="W",
            minimum=Decimal(1),
            maximum=Decimal(2500),
        ),
    ),
    formula=_ftp_20_min_95,
)

COOPER_12_MIN_V1 = MethodDefinition(
    metric_code="VO2MAX_ESTIMATED",
    label_key="training_metric.vo2max_estimated",
    result_unit="mL/kg/min",
    result_scale=1,
    method_code="COOPER_12_MIN_V1",
    method_version=1,
    method_label_key="training_metric.method.cooper_12_min_v1",
    inputs=(
        InputDefinition(
            code="distance_meters",
            label_key="training_metric.input.distance_meters",
            unit="m",
            minimum=Decimal(600),
            maximum=Decimal(6000),
        ),
    ),
    formula=_cooper_12_min,
)

TRAINING_METRICS_CATALOG = MetricCatalog(
    catalog_version="training-metrics-2026-10-12.v1",
    methods=(FTP_20_MIN_95_V1, COOPER_12_MIN_V1),
)
