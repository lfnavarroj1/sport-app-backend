"""HU034 sin infraestructura: catálogo, fórmulas decimales y validación (ADR-030, CA1-CA3)."""

import unittest
from datetime import timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid1, uuid4

from fakes import NOW
from metric_fakes import EXTENDED_TEST_CATALOG, cooper_body, ftp_body, recovery_body

from training_management.domain.metrics.catalog import (
    COOPER_12_MIN_V1,
    FTP_20_MIN_95_V1,
    TRAINING_METRICS_CATALOG,
    InputDefinition,
    MetricCatalog,
)
from training_management.domain.metrics.events import (
    AGGREGATE_TYPE,
    EVENT_TYPE,
    SCHEMA_VERSION,
    calculated_payload,
)
from training_management.domain.metrics.model import (
    TrainingMetricCalculation,
    json_number,
    validate_calculation,
)
from training_management.domain.metrics.validation import InvalidMetricCalculation


def issues(
    body: object, catalog: MetricCatalog = TRAINING_METRICS_CATALOG
) -> set[tuple[str | None, str]]:
    try:
        validate_calculation(body, catalog, NOW)
    except InvalidMetricCalculation as error:
        return {(item.field, item.issue.value) for item in error.violations}
    return set()


def calculate(body: dict[str, Any], catalog: MetricCatalog = TRAINING_METRICS_CATALOG) -> Any:
    validated = validate_calculation(body, catalog, NOW)
    return TrainingMetricCalculation.calculate(athlete_id=uuid4(), validated=validated, now=NOW)


def ftp(watts: int) -> Decimal:
    return FTP_20_MIN_95_V1.calculate({"mean_power_watts": Decimal(watts)})


def cooper(meters: int) -> Decimal:
    return COOPER_12_MIN_V1.calculate({"distance_meters": Decimal(meters)})


class FormulaTest(unittest.TestCase):
    def test_exact_vectors_of_the_adr(self) -> None:
        """CA1 y CA2: 250 W → 238 W (escala 0); 2.400 m → 42,4 mL/kg/min (escala 1)."""
        self.assertEqual(str(ftp(250)), "238")
        self.assertEqual(str(cooper(2400)), "42.4")

    def test_half_values_round_half_up(self) -> None:
        # 10 * 0,95 = 9,5 → 10; 30 * 0,95 = 28,5 → 29 (HALF_EVEN daría 28); 50 * 0,95 → 48.
        self.assertEqual([str(ftp(watts)) for watts in (10, 30, 50)], ["10", "29", "48"])
        # (2741 - 504,9) / 44,73 = 49,991… → 50,0 (conserva el cero de la escala).
        self.assertEqual(str(cooper(2741)), "50.0")
        # Resultado exacto 49,85 (con HALF_EVEN sería 49,8): HALF_UP da 49,9.
        exact_half = Decimal("504.9") + Decimal("44.73") * Decimal("49.85")
        self.assertEqual(str(COOPER_12_MIN_V1.calculate({"distance_meters": exact_half})), "49.9")

    def test_minimum_and_maximum_inputs(self) -> None:
        self.assertEqual((str(ftp(1)), str(ftp(2500))), ("1", "2375"))
        self.assertEqual((str(cooper(600)), str(cooper(6000))), ("2.1", "122.9"))

    def test_results_are_decimal_with_the_method_scale_and_deterministic(self) -> None:
        for watts in (1, 199, 250, 1333, 2500):
            first, second = ftp(watts), ftp(watts)
            self.assertIsInstance(first, Decimal)
            self.assertEqual((first, first.as_tuple().exponent), (second, 0))
        for meters in (600, 1777, 2400, 6000):
            self.assertEqual(cooper(meters).as_tuple().exponent, -1)
            self.assertEqual(cooper(meters), cooper(meters))

    def test_json_numbers_keep_the_scale(self) -> None:
        self.assertEqual(json_number(Decimal("238.0000"), 0), 238)
        self.assertIsInstance(json_number(Decimal("238"), 0), int)
        self.assertEqual(json_number(Decimal("42.4000"), 1), 42.4)
        self.assertEqual(repr(json_number(Decimal("40"), 1)), "40.0")


class CatalogTest(unittest.TestCase):
    def test_initial_catalog_has_exactly_the_two_adr_methods(self) -> None:
        catalog = TRAINING_METRICS_CATALOG
        self.assertEqual(catalog.catalog_version, "training-metrics-2026-10-12.v1")
        self.assertEqual(catalog.etag, '"training-metrics-2026-10-12.v1"')
        self.assertEqual(
            [(m.metric_code, m.method_code, m.method_version) for m in catalog.methods],
            [
                ("FTP_ESTIMATED", "FTP_20_MIN_95_V1", 1),
                ("VO2MAX_ESTIMATED", "COOPER_12_MIN_V1", 1),
            ],
        )
        self.assertEqual(
            [(m.result_unit, m.result_scale, m.disclaimer_key) for m in catalog.methods],
            [("W", 0, "estimated_not_diagnostic"), ("mL/kg/min", 1, "estimated_not_diagnostic")],
        )
        self.assertEqual(
            [
                (i.code, i.unit, i.minimum, i.maximum, i.value_type, i.required)
                for m in catalog.methods
                for i in m.inputs
            ],
            [
                ("mean_power_watts", "W", 1, 2500, "INTEGER", True),
                ("distance_meters", "m", 600, 6000, "INTEGER", True),
            ],
        )

    def test_catalog_rejects_inconsistent_definitions(self) -> None:
        duplicated = (FTP_20_MIN_95_V1, FTP_20_MIN_95_V1)
        decimal_input = InputDefinition("x", "k", "u", Decimal(0), Decimal(1), scale=1)
        cases: list[Any] = [
            duplicated,
            (FTP_20_MIN_95_V1.__class__(**{**FTP_20_MIN_95_V1.__dict__, "inputs": ()}),),
            (
                FTP_20_MIN_95_V1.__class__(
                    **{**FTP_20_MIN_95_V1.__dict__, "inputs": (decimal_input,)}
                ),
            ),
            (FTP_20_MIN_95_V1.__class__(**{**FTP_20_MIN_95_V1.__dict__, "result_scale": 5}),),
        ]
        for methods in cases:
            with self.subTest(methods=len(methods)), self.assertRaises(ValueError):
                MetricCatalog("x", methods)


class CalculationRulesTest(unittest.TestCase):
    def test_valid_requests_are_normalized(self) -> None:
        calculation = calculate(ftp_body())
        vo2 = calculate(cooper_body())

        self.assertEqual((calculation.result.value, calculation.result.unit), (238, "W"))
        self.assertEqual(
            (vo2.result.value, vo2.result.unit, vo2.result.scale), (Decimal("42.4"), "mL/kg/min", 1)
        )
        self.assertEqual((calculation.status.value, calculation.version), ("CONFIRMED", 1))
        self.assertEqual(calculation.calculated_at, NOW)
        self.assertEqual(calculation.disclaimer_key, "estimated_not_diagnostic")

    def test_server_owned_and_unknown_fields_are_not_allowed(self) -> None:
        body = ftp_body(athlete_id=str(uuid4()), result=238, status="CONFIRMED")
        body["inputs"][0]["scale"] = 0

        self.assertEqual(
            issues(body),
            {
                ("athlete_id", "field_not_allowed"),
                ("result", "field_not_allowed"),
                ("status", "field_not_allowed"),
                ("inputs[0].scale", "field_not_allowed"),
            },
        )

    def test_required_fields(self) -> None:
        self.assertEqual(issues(None), {(None, "required")})
        self.assertEqual(issues([]), {(None, "required")})
        self.assertEqual(
            issues({}),
            {
                ("calculation_id", "required"),
                ("metric_code", "required"),
                ("method_code", "required"),
                ("method_version", "required"),
                ("measured_at", "required"),
                ("inputs", "required"),
            },
        )
        self.assertEqual(issues(ftp_body(inputs=[])), {("inputs", "required")})
        self.assertEqual(issues(ftp_body(inputs="x")), {("inputs", "required")})
        self.assertEqual(
            issues(ftp_body(inputs=[{}])),
            {
                ("inputs[0].code", "required"),
                ("inputs[0].unit", "required"),
                ("inputs[0].value", "required"),
                ("inputs", "required"),
            },
        )

    def test_metric_method_and_version(self) -> None:
        cases: dict[tuple[str, str], dict[str, Any]] = {
            ("metric_code", "unknown_metric"): ftp_body(metric_code="FTP"),
            ("method_code", "unsupported_method_version"): ftp_body(method_code="RAMP_V1"),
            ("method_version", "unsupported_method_version"): ftp_body(method_version=2),
        }
        for expected, body in cases.items():
            with self.subTest(expected=expected):
                self.assertEqual(issues(body), {expected})
        # Método de otro indicador (incompatible) y tipos JSON incorrectos.
        self.assertEqual(
            issues(ftp_body(method_code="COOPER_12_MIN_V1")),
            {("method_code", "unsupported_method_version")},
        )
        self.assertEqual(
            issues(ftp_body(metric_code=1, method_code=2, method_version=True)),
            {
                ("metric_code", "unknown_metric"),
                ("method_code", "unsupported_method_version"),
                ("method_version", "unsupported_method_version"),
            },
        )
        self.assertEqual(
            issues(ftp_body(method_version="1")), {("method_version", "unsupported_method_version")}
        )

    def test_inputs_unknown_duplicated_unit_and_range(self) -> None:
        power = {"code": "mean_power_watts", "value": 250, "unit": "W"}
        cases: dict[str, tuple[list[Any], set[tuple[str | None, str]]]] = {
            "desconocida": (
                [{"code": "distance_meters", "value": 2400, "unit": "m"}],
                {("inputs[0].code", "unknown_input"), ("inputs", "required")},
            ),
            "duplicada": ([power, power], {("inputs[1].code", "duplicate_input")}),
            "unidad": ([{**power, "unit": "kW"}], {("inputs[0].unit", "invalid_unit")}),
            "unidad no texto": ([{**power, "unit": 1}], {("inputs[0].unit", "invalid_unit")}),
            "no objeto": ([power, "x"], {("inputs[1]", "unknown_input")}),
            "código no texto": (
                [{**power, "code": 7}],
                {("inputs[0].code", "unknown_input"), ("inputs", "required")},
            ),
        }
        for name, (inputs, expected) in cases.items():
            with self.subTest(name=name):
                self.assertEqual(issues(ftp_body(inputs=inputs)), expected)

    def test_values_out_of_range_or_not_integers(self) -> None:
        for value in (0, 2501, -1, 250.5, 250.0, "250", True, [250], 10**40):
            with self.subTest(value=value):
                self.assertEqual(
                    issues(
                        ftp_body(inputs=[{"code": "mean_power_watts", "value": value, "unit": "W"}])
                    ),
                    {("inputs[0].value", "out_of_range")},
                )
        for meters in (599, 6001):
            with self.subTest(meters=meters):
                body = cooper_body(
                    inputs=[{"code": "distance_meters", "value": meters, "unit": "m"}]
                )
                self.assertEqual(issues(body), {("inputs[0].value", "out_of_range")})
        for watts in (1, 2500):
            body = ftp_body(inputs=[{"code": "mean_power_watts", "value": watts, "unit": "W"}])
            self.assertEqual(issues(body), set())

    def test_measured_at_format_and_window(self) -> None:
        def at(delta: timedelta) -> str:
            return (NOW + delta).isoformat().replace("+00:00", "Z")

        cases = {
            "2026-10-12T11:30:00+00:00": "invalid_datetime",
            "2026-10-12T11:30:00.500Z": "invalid_datetime",
            "2026-10-12 11:30:00Z": "invalid_datetime",
            "2026-02-30T11:30:00Z": "invalid_datetime",
            "2026-10-12T11:30:00": "invalid_datetime",
            at(timedelta(minutes=5, seconds=1)): "too_far_in_future",
            at(-timedelta(days=365, seconds=1)): "too_old",
        }
        for value, issue in cases.items():
            with self.subTest(value=value):
                self.assertEqual(issues(ftp_body(measured_at=value)), {("measured_at", issue)})
        self.assertEqual(issues(ftp_body(measured_at=1)), {("measured_at", "invalid_datetime")})
        for limit in (timedelta(minutes=5), -timedelta(days=365)):
            self.assertEqual(issues(ftp_body(measured_at=at(limit))), set())

    def test_calculation_id_must_be_uuid_v4(self) -> None:
        for value in ("x", str(uuid1()), 7):
            with self.subTest(value=value):
                self.assertEqual(
                    issues(ftp_body(calculation_id=value)), {("calculation_id", "invalid_uuid")}
                )

    def test_input_order_does_not_change_the_normalized_data(self) -> None:
        body = recovery_body()
        reversed_body = {**body, "inputs": list(reversed(body["inputs"]))}

        first = validate_calculation(body, EXTENDED_TEST_CATALOG, NOW).data
        second = validate_calculation(reversed_body, EXTENDED_TEST_CATALOG, NOW).data

        self.assertEqual(first, second)
        self.assertEqual(first.fingerprint, second.fingerprint)
        self.assertEqual(
            [item.code for item in first.inputs], ["peak_heart_rate_bpm", "recovery_heart_rate_bpm"]
        )


class EventPayloadTest(unittest.TestCase):
    def test_payload_carries_the_complete_confirmed_state(self) -> None:
        body = cooper_body()
        calculation = calculate(body)

        payload = calculated_payload(calculation)

        self.assertEqual(
            (EVENT_TYPE, SCHEMA_VERSION, AGGREGATE_TYPE),
            ("indicador.calculado.v1", 1, "training_metric_calculation"),
        )
        self.assertEqual(
            payload,
            {
                "calculation_id": body["calculation_id"],
                "athlete_id": str(calculation.athlete_id),
                "metric_code": "VO2MAX_ESTIMATED",
                "method_code": "COOPER_12_MIN_V1",
                "method_version": 1,
                "measured_at": "2026-10-12T11:00:00Z",
                "calculated_at": "2026-10-12T12:00:00Z",
                "inputs": [{"code": "distance_meters", "value": 2400, "unit": "m"}],
                "result": {"value": 42.4, "unit": "mL/kg/min", "scale": 1},
                "status": "CONFIRMED",
                "disclaimer_key": "estimated_not_diagnostic",
            },
        )
        self.assertEqual(calculated_payload(calculate(ftp_body()))["result"]["value"], 238)


class ExtensibilityTest(unittest.TestCase):
    """HA014: un tercer método escalar se agrega solo en un catálogo de prueba."""

    def test_new_scalar_method_validates_and_calculates_without_other_changes(self) -> None:
        calculation = calculate(recovery_body(), EXTENDED_TEST_CATALOG)

        self.assertEqual((calculation.result.value, calculation.result.unit), (32, "bpm"))
        self.assertEqual(
            issues(recovery_body(inputs=[recovery_body()["inputs"][0]]), EXTENDED_TEST_CATALOG),
            {("inputs", "required")},
        )
        # El catálogo del producto no lo conoce.
        self.assertEqual(issues(recovery_body()), {("metric_code", "unknown_metric")})


if __name__ == "__main__":
    unittest.main()
