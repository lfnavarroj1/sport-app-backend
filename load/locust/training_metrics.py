"""HA014: catálogo, cálculo y consulta de FTP y VO₂max a través del BFF móvil (HU034, ADR-030).

Mezcla 30 % `GET /v1/training-metrics/catalog`, 35 % `POST /v1/training-metrics/calculations`
y 35 % `GET /v1/training-metrics/calculations` a una tasa constante. Tras el calentamiento se
reinician las estadísticas y, al terminar, cada operación debe cumplir p95 < 3 s y error < 1 %;
si no, el proceso termina con código 1. Los rechazos funcionales esperados (códigos estables
de ADR-030 con `403`, `409` o `422`) se registran aparte y no cuentan como error ni como
latencia de la operación.

Uso (fuera de cada PR; datos sintéticos). Corrida completa de HA014 (10 min a 30 rps):
    uv run --group load locust -f load/locust/training_metrics.py --host http://127.0.0.1:8000 \
        --headless --csv reports/load/ha014 --only-summary

Parámetros (variables de entorno; la forma de carga ignora `--users` y `--run-time`):
    HA014_RPS                   solicitudes por segundo en total (30)
    HA014_USER_RPS              solicitudes por segundo de cada usuario virtual (1); los
                                usuarios son ceil(HA014_RPS / HA014_USER_RPS)
    HA014_WARMUP_SECONDS        calentamiento antes de medir (60); no cuenta en los umbrales
    HA014_DURATION_SECONDS      ventana medida (600)
    HA014_P95_MS                umbral de p95 por operación en ms (3000)
    HA014_MAX_ERROR_RATIO       umbral de error por operación (0.01)

Dataset: deportistas con un plan que incluya `training.metrics.ftp_vo2max` (el `FREE` la trae).
    HA014_ATHLETES_FILE         CSV `email,password` sin encabezado; los usuarios virtuales se
                                reparten las cuentas en rotación. Si falta, cada usuario virtual
                                registra su propio deportista sintético en:
    HA014_REGISTRATION_URL      `POST /v1/users` de `users-management` o de `bff-web` (mismo
                                contrato); después espera en `GET /v1/me/capabilities` del BFF
                                móvil a que el alta gratuita asíncrona active la capacidad.
    REGISTRATION_POLICIES_VERSION   la configurada en `users-management` (local-dev)
    HA014_PROVISIONING_TIMEOUT_SECONDS  espera máxima de la capacidad (120)

La evidencia debe registrar revisión, dataset, recursos, p50/p95/p99 y tasa de error (ADR-030).
"""

import csv
import itertools
import logging
import math
import os
import random
import threading
import time
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

from locust import HttpUser, LoadTestShape, constant_throughput, events, task
from locust.env import Environment
from locust.exception import StopUser

RPS = float(os.environ.get("HA014_RPS", "30"))
USER_RPS = float(os.environ.get("HA014_USER_RPS", "1"))
WARMUP_SECONDS = float(os.environ.get("HA014_WARMUP_SECONDS", "60"))
DURATION_SECONDS = float(os.environ.get("HA014_DURATION_SECONDS", "600"))
P95_MS = float(os.environ.get("HA014_P95_MS", "3000"))
MAX_ERROR_RATIO = float(os.environ.get("HA014_MAX_ERROR_RATIO", "0.01"))
ATHLETES_FILE = os.environ.get("HA014_ATHLETES_FILE", "")
REGISTRATION_URL = os.environ.get("HA014_REGISTRATION_URL", "").rstrip("/")
POLICIES_VERSION = os.environ.get("REGISTRATION_POLICIES_VERSION", "local-dev")
PROVISIONING_TIMEOUT = float(os.environ.get("HA014_PROVISIONING_TIMEOUT_SECONDS", "120"))

CAPABILITY = "training.metrics.ftp_vo2max"
CATALOG = "/v1/training-metrics/catalog"
CALCULATIONS = "/v1/training-metrics/calculations"
OPERATIONS = (("GET", CATALOG), ("POST", CALCULATIONS), ("GET", CALCULATIONS))
# Rechazos funcionales esperados de ADR-030; cualquier otro estado es error.
FUNCTIONAL_REJECTIONS = {
    403: {"capability_required"},
    409: {"idempotency_key_reused", "calculation_id_reused"},
    422: {"validation_error"},
}
# El access token dura 15 min (HU003): se renueva antes de que venza durante la corrida.
TOKEN_RENEWAL_SECONDS = 12 * 60
NOT_MODIFIED = 304
# La corrida no es válida si no sostiene al menos el 90 % de la tasa pedida.
MIN_RATE_RATIO = 0.9

_accounts_lock = threading.Lock()
_accounts: Iterator[tuple[str, str]] | None = None


def _next_account() -> tuple[str, str] | None:
    global _accounts  # noqa: PLW0603
    if not ATHLETES_FILE:
        return None
    with _accounts_lock:
        if _accounts is None:
            with open(ATHLETES_FILE, encoding="utf-8", newline="") as source:
                rows = [(row[0].strip(), row[1]) for row in csv.reader(source) if row]
            if not rows:
                raise RuntimeError("HA014_ATHLETES_FILE no tiene cuentas")
            _accounts = itertools.cycle(rows)
        return next(_accounts)


def _utc(instant: datetime) -> str:
    return instant.replace(microsecond=0).isoformat().replace("+00:00", "Z")


class Ha014Shape(LoadTestShape):
    """Tasa constante: todos los usuarios desde el inicio; mide después del calentamiento."""

    _reset = False

    def tick(self) -> tuple[int, float] | None:
        elapsed = self.get_run_time()
        if elapsed >= WARMUP_SECONDS + DURATION_SECONDS:
            return None
        if not self._reset and elapsed >= WARMUP_SECONDS and self.runner is not None:
            self.runner.stats.reset_all()
            self._reset = True
        users = max(1, math.ceil(RPS / USER_RPS))
        return users, float(users)


class MetricsAthlete(HttpUser):
    wait_time = constant_throughput(USER_RPS)

    def on_start(self) -> None:
        self.etag: str | None = None
        try:
            account = _next_account()
            if account is None:
                account = self._register()
            self.email, self.password = account
            self._login()
            if not ATHLETES_FILE:
                self._wait_for_capability()
        except Exception as error:
            # Sin deportista listo no hay carga válida: el usuario se detiene y la tasa lo delata.
            status = getattr(getattr(error, "response", None), "status_code", None)
            logging.getLogger("ha014").error(
                "preparación fallida: %s (estado %s)", type(error).__name__, status
            )
            raise StopUser from error

    def _register(self) -> tuple[str, str]:
        if not REGISTRATION_URL:
            raise RuntimeError("defina HA014_ATHLETES_FILE o HA014_REGISTRATION_URL")
        unique = uuid.uuid4().hex
        # email-validator rechaza dominios reservados como `.test`.
        email, password = f"ha014-{unique}@example.com", f"clave-sintetica-{unique}"
        self.client.post(
            f"{REGISTRATION_URL}/v1/users",
            name="setup: POST /v1/users",
            headers={"Idempotency-Key": str(uuid.uuid4())},
            json={
                "full_name": "Deportista Sintético",
                "email": email,
                "password": password,
                "actor_type": "athlete",
                "accepted_policies_version": POLICIES_VERSION,
            },
        ).raise_for_status()
        return email, password

    def _login(self) -> None:
        response = self.client.post(
            "/v1/sessions",
            name="setup: POST /v1/sessions",
            json={"email": self.email, "password": self.password},
        )
        response.raise_for_status()
        self.headers = {"Authorization": f"Bearer {response.json()['access_token']}"}
        self.logged_in_at = time.monotonic()

    def _wait_for_capability(self) -> None:
        deadline = time.monotonic() + PROVISIONING_TIMEOUT
        while time.monotonic() < deadline:
            response = self.client.get(
                "/v1/me/capabilities", name="setup: GET /v1/me/capabilities", headers=self.headers
            )
            if response.ok and CAPABILITY in response.json().get("capabilities", []):
                return
            time.sleep(1)
        raise RuntimeError(f"el deportista no recibió {CAPABILITY} a tiempo")

    def _renew_if_needed(self) -> None:
        if time.monotonic() - self.logged_in_at > TOKEN_RENEWAL_SECONDS:
            self._login()

    def _send(self, method: str, path: str, expected: set[int], **kwargs: Any) -> Any:
        self._renew_if_needed()
        headers = {**self.headers, **kwargs.pop("headers", {})}
        with self.client.request(
            method, path, name=path, headers=headers, catch_response=True, **kwargs
        ) as response:
            if response.status_code in expected:
                response.success()
                return response
            code = _error_code(response)
            if code in FUNCTIONAL_REJECTIONS.get(response.status_code, set()):
                # Fuera de la latencia y del error de la operación, pero visible en el informe.
                response.request_meta["name"] = (
                    f"rechazo funcional: {method} {path} {response.status_code} {code}"
                )
                response.success()
            else:
                response.failure(f"{response.status_code} {code or 'sin código'}")
            return response

    @task(30)
    def catalog(self) -> None:
        headers = {"If-None-Match": self.etag} if self.etag else {}
        response = self._send("GET", CATALOG, {200, NOT_MODIFIED}, headers=headers)
        self.etag = response.headers.get("ETag") or self.etag

    @task(35)
    def calculate(self) -> None:
        if random.random() < 0.5:  # noqa: PLR2004
            metric, method, code, value, unit = (
                "FTP_ESTIMATED",
                "FTP_20_MIN_95_V1",
                "mean_power_watts",
                random.randint(120, 400),
                "W",
            )
        else:
            metric, method, code, value, unit = (
                "VO2MAX_ESTIMATED",
                "COOPER_12_MIN_V1",
                "distance_meters",
                random.randint(1600, 3600),
                "m",
            )
        measured_at = datetime.now(UTC) - timedelta(minutes=random.randint(10, 60 * 24 * 30))
        self._send(
            "POST",
            CALCULATIONS,
            {200, 201},
            headers={"Idempotency-Key": str(uuid.uuid4())},
            json={
                "calculation_id": str(uuid.uuid4()),
                "metric_code": metric,
                "method_code": method,
                "method_version": 1,
                "measured_at": _utc(measured_at),
                "inputs": [{"code": code, "value": value, "unit": unit}],
            },
        )

    @task(35)
    def history(self) -> None:
        params: dict[str, Any] = {"limit": 20}
        if random.random() < 0.3:  # noqa: PLR2004
            params["metric_code"] = random.choice(("FTP_ESTIMATED", "VO2MAX_ESTIMATED"))
        self._send("GET", CALCULATIONS, {200}, params=params)


def _error_code(response: Any) -> str | None:
    try:
        body = response.json()
    except ValueError:
        return None
    code = body.get("code") if isinstance(body, dict) else None
    return code if isinstance(code, str) else None


@events.quitting.add_listener
def _check_thresholds(environment: Environment, **_: Any) -> None:
    """Imprime p50/p95/p99 y error por operación y falla si se incumple HA014."""
    failed = False
    print(f"HA014 (p95 < {P95_MS:.0f} ms, error < {MAX_ERROR_RATIO:.2%}) tras el calentamiento:")
    for method, path in OPERATIONS:
        entry = environment.stats.get(path, method)
        if entry.num_requests == 0:
            print(f"  {method} {path}: sin solicitudes")
            failed = True
            continue
        p95 = entry.get_response_time_percentile(0.95)
        print(
            f"  {method} {path}: n={entry.num_requests} "
            f"p50={entry.get_response_time_percentile(0.5):.0f} ms p95={p95:.0f} ms "
            f"p99={entry.get_response_time_percentile(0.99):.0f} ms "
            f"error={entry.fail_ratio:.2%} rps={entry.total_rps:.1f}"
        )
        failed = failed or p95 >= P95_MS or entry.fail_ratio >= MAX_ERROR_RATIO
    for entry in environment.stats.entries.values():
        if entry.name.startswith("rechazo funcional:"):
            print(f"  {entry.name}: n={entry.num_requests} (excluido)")
    measured_rps = sum(environment.stats.get(path, method).total_rps for method, path in OPERATIONS)
    print(f"  tasa medida: {measured_rps:.1f} rps (objetivo {RPS:.1f})")
    if measured_rps < RPS * MIN_RATE_RATIO:
        print("  la tasa medida no alcanzó el objetivo: faltan usuarios o el sistema se saturó")
        failed = True
    if failed:
        print("HA014 NO cumple los umbrales")
        environment.process_exit_code = 1
    else:
        print("HA014 cumple los umbrales")
