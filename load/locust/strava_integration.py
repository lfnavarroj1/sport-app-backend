"""HA012: operaciones sincrónicas de la integración con Strava por el BFF móvil (HU031, ADR-042).

Mezcla 30 % `POST /v1/me/integrations/strava/authorization`, 40 % `GET
/v1/me/integrations/strava` y 30 % `POST /v1/me/integrations/strava/syncs` a una tasa
constante. La importación es asíncrona (`terceros-sync-worker`): aquí solo se mide la
respuesta sincrónica. Tras el calentamiento se reinician las estadísticas y, al terminar, cada
operación debe cumplir p95 < 2 s y error < 1 %; si no, el proceso termina con código 1.

En la sincronización, `409 sync_in_progress` (ya hay una corrida `QUEUED`/`RUNNING` del
deportista) y `409 integration_not_linked` (cuenta sin vincular) son respuestas correctas del
contrato: cuentan en la latencia y no como error. Cualquier otro estado es error. La carga
nunca llama a Strava real: `third-party-management` debe apuntar al simulador o a un ambiente
sin `terceros-sync-worker`; la autorización solo crea el `state` y devuelve la URL.

Uso (fuera de cada PR; datos sintéticos). Corrida de HA012 (10 min a 20 rps):
    uv run --group load locust -f load/locust/strava_integration.py --host http://127.0.0.1:8000 \
        --headless --csv reports/load/ha012 --only-summary

Parámetros (variables de entorno; la forma de carga ignora `--users` y `--run-time`):
    HA012_RPS                   solicitudes por segundo en total (20)
    HA012_USER_RPS              solicitudes por segundo de cada usuario virtual (1); los
                                usuarios son ceil(HA012_RPS / HA012_USER_RPS)
    HA012_WARMUP_SECONDS        calentamiento antes de medir (60); no cuenta en los umbrales
    HA012_DURATION_SECONDS      ventana medida (600)
    HA012_P95_MS                umbral de p95 por operación en ms (2000)
    HA012_MAX_ERROR_RATIO       umbral de error por operación (0.01)

Dataset: deportistas con un plan que incluya `integrations.strava.connect` (el `FREE` la trae).
    HA012_ATHLETES_FILE         CSV `email,password` sin encabezado; los usuarios virtuales se
                                reparten las cuentas en rotación (vinculadas o no). Si falta,
                                cada usuario virtual registra su propio deportista sintético en:
    HA012_REGISTRATION_URL      `POST /v1/users` de `users-management` o de `bff-web` (mismo
                                contrato); después espera en `GET /v1/me/capabilities` del BFF
                                móvil a que el alta gratuita asíncrona active la capacidad.
    REGISTRATION_POLICIES_VERSION   la configurada en `users-management` (local-dev)
    HA012_PROVISIONING_TIMEOUT_SECONDS  espera máxima de la capacidad (120)

La evidencia debe registrar revisión, dataset, recursos, p50/p95/p99 y tasa de error (EXP-002).
"""

import csv
import itertools
import logging
import math
import os
import threading
import time
import uuid
from collections.abc import Iterator
from typing import Any

from locust import HttpUser, LoadTestShape, constant_throughput, events, task
from locust.env import Environment
from locust.exception import StopUser

RPS = float(os.environ.get("HA012_RPS", "20"))
USER_RPS = float(os.environ.get("HA012_USER_RPS", "1"))
WARMUP_SECONDS = float(os.environ.get("HA012_WARMUP_SECONDS", "60"))
DURATION_SECONDS = float(os.environ.get("HA012_DURATION_SECONDS", "600"))
P95_MS = float(os.environ.get("HA012_P95_MS", "2000"))
MAX_ERROR_RATIO = float(os.environ.get("HA012_MAX_ERROR_RATIO", "0.01"))
ATHLETES_FILE = os.environ.get("HA012_ATHLETES_FILE", "")
REGISTRATION_URL = os.environ.get("HA012_REGISTRATION_URL", "").rstrip("/")
POLICIES_VERSION = os.environ.get("REGISTRATION_POLICIES_VERSION", "local-dev")
PROVISIONING_TIMEOUT = float(os.environ.get("HA012_PROVISIONING_TIMEOUT_SECONDS", "120"))

CAPABILITY = "integrations.strava.connect"
STRAVA = "/v1/me/integrations/strava"
AUTHORIZATION = f"{STRAVA}/authorization"
SYNCS = f"{STRAVA}/syncs"
OPERATIONS = (("POST", AUTHORIZATION), ("GET", STRAVA), ("POST", SYNCS))
# Respuestas `409` del contrato de la sincronización (ADR-042); cualquier otra es error.
SYNC_CONFLICTS = {"sync_in_progress", "integration_not_linked"}
CONFLICT = 409
# El access token dura 15 min (HU003): se renueva antes de que venza durante la corrida.
TOKEN_RENEWAL_SECONDS = 12 * 60
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
                raise RuntimeError("HA012_ATHLETES_FILE no tiene cuentas")
            _accounts = itertools.cycle(rows)
        return next(_accounts)


class Ha012Shape(LoadTestShape):
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


class StravaAthlete(HttpUser):
    wait_time = constant_throughput(USER_RPS)

    def on_start(self) -> None:
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
            logging.getLogger("ha012").error(
                "preparación fallida: %s (estado %s)", type(error).__name__, status
            )
            raise StopUser from error

    def _register(self) -> tuple[str, str]:
        if not REGISTRATION_URL:
            raise RuntimeError("defina HA012_ATHLETES_FILE o HA012_REGISTRATION_URL")
        unique = uuid.uuid4().hex
        # email-validator rechaza dominios reservados como `.test`.
        email, password = f"ha012-{unique}@example.com", f"clave-sintetica-{unique}"
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

    def _send(
        self,
        method: str,
        path: str,
        expected: set[int],
        conflicts: set[str] | None = None,
        **kwargs: Any,
    ) -> None:
        self._renew_if_needed()
        headers = {**self.headers, **kwargs.pop("headers", {})}
        with self.client.request(
            method, path, name=path, headers=headers, catch_response=True, **kwargs
        ) as response:
            code = None if response.status_code in expected else _error_code(response)
            if response.status_code in expected or (
                response.status_code == CONFLICT and code in (conflicts or set())
            ):
                response.success()
            else:
                response.failure(f"{response.status_code} {code or 'sin código'}")

    @task(30)
    def authorize(self) -> None:
        self._send("POST", AUTHORIZATION, {201})

    @task(40)
    def status(self) -> None:
        self._send("GET", STRAVA, {200})

    @task(30)
    def sync(self) -> None:
        self._send(
            "POST",
            SYNCS,
            {202},
            SYNC_CONFLICTS,
            headers={"Idempotency-Key": str(uuid.uuid4())},
        )


def _error_code(response: Any) -> str | None:
    try:
        body = response.json()
    except ValueError:
        return None
    code = body.get("code") if isinstance(body, dict) else None
    return code if isinstance(code, str) else None


@events.quitting.add_listener
def _check_thresholds(environment: Environment, **_: Any) -> None:
    """Imprime p50/p95/p99 y error por operación y falla si se incumple HA012."""
    failed = False
    print(f"HA012 (p95 < {P95_MS:.0f} ms, error < {MAX_ERROR_RATIO:.2%}) tras el calentamiento:")
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
    measured_rps = sum(environment.stats.get(path, method).total_rps for method, path in OPERATIONS)
    print(f"  tasa medida: {measured_rps:.1f} rps (objetivo {RPS:.1f})")
    if measured_rps < RPS * MIN_RATE_RATIO:
        print("  la tasa medida no alcanzó el objetivo: faltan usuarios o el sistema se saturó")
        failed = True
    if failed:
        print("HA012 NO cumple los umbrales")
        environment.process_exit_code = 1
    else:
        print("HA012 cumple los umbrales")
