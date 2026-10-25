"""HA018: reservar una sesión virtual en menos de 2 s p95 bajo concurrencia (HU040, ADR-046 §9).

Dos ráfagas sobre `POST /v1/me/appointments` del BFF móvil, cada usuario virtual con su
propio deportista `PREMIUM` y una sola reserva:
- `distinct`: HA018_DISTINCT usuarios (50) reservan a la vez intervalos distintos; todas deben
  confirmarse (`201`).
- `contended`: HA018_CONTENDED usuarios (20) reservan a la vez el mismo intervalo de capacidad
  1; exactamente una se confirma y las demás reciben `409 availability_full`.
Al terminar, el `POST` debe cumplir p95 < HA018_P95_MS (2000) incluyendo los `409`, que son la
respuesta correcta del contrato; cualquier otro estado es error. Si no, el proceso termina con
código 1. Los `GET` de preparación no cuentan.

Uso (fuera de cada PR; datos sintéticos). Se lanza con tantos usuarios como cuentas y una tasa
de aparición igual, para que la ráfaga sea simultánea:
    HA018_ATHLETES_FILE=deportistas.csv HA018_SLOTS_FILE=intervalos.csv \
    HA018_CONTENDED_SLOT=<service_id>,<availability_id> \
    uv run --group load locust -f load/locust/appointments.py --host http://127.0.0.1:8000 \
        --headless --users 70 --spawn-rate 70 --run-time 2m --csv reports/load/ha018 \
        --only-summary

Dataset (variables de entorno):
    HA018_ATHLETES_FILE   CSV `email,password` sin encabezado de deportistas con
                          `services.virtual_sessions.schedule` (`PREMIUM`); al menos
                          HA018_DISTINCT + HA018_CONTENDED cuentas, una por usuario virtual.
    HA018_SLOTS_FILE      CSV `service_id,availability_id` de intervalos distintos de
                          servicios `COACHING` `VIRTUAL`/`HYBRID` publicados, que empiecen en
                          más de 1 h; al menos HA018_DISTINCT filas.
    HA018_CONTENDED_SLOT  `service_id,availability_id` de un intervalo de capacidad 1 libre.
    HA018_DISTINCT        usuarios de la ráfaga `distinct` (50)
    HA018_CONTENDED       usuarios de la ráfaga `contended` (20)
    HA018_P95_MS          umbral de p95 del `POST` en ms (2000)

La evidencia debe registrar revisión, dataset, recursos, p50/p95/p99, confirmadas y `409`.
"""

import csv
import itertools
import logging
import os
import threading
import uuid
from collections import Counter
from typing import Any

from locust import HttpUser, constant, events, task
from locust.env import Environment
from locust.exception import StopUser

ATHLETES_FILE = os.environ.get("HA018_ATHLETES_FILE", "")
SLOTS_FILE = os.environ.get("HA018_SLOTS_FILE", "")
CONTENDED_SLOT = os.environ.get("HA018_CONTENDED_SLOT", "")
DISTINCT = int(os.environ.get("HA018_DISTINCT", "50"))
CONTENDED = int(os.environ.get("HA018_CONTENDED", "20"))
P95_MS = float(os.environ.get("HA018_P95_MS", "2000"))
APPOINTMENTS = "/v1/me/appointments"
CREATED, CONFLICT = 201, 409
FULL = (CONFLICT, "availability_full")

_lock = threading.Lock()
_counter = itertools.count()
_outcomes: Counter[tuple[str, int, str | None]] = Counter()
_rows: dict[str, list[list[str]]] = {}


def _csv(path: str) -> list[list[str]]:
    with _lock:
        if path not in _rows:
            with open(path, encoding="utf-8", newline="") as source:
                _rows[path] = [[cell.strip() for cell in row] for row in csv.reader(source) if row]
        return _rows[path]


def _assignment() -> tuple[int, tuple[str, str], str, tuple[str, str]]:
    """Índice del usuario virtual, su cuenta, su ráfaga y su intervalo."""
    if not (ATHLETES_FILE and SLOTS_FILE and CONTENDED_SLOT):
        raise RuntimeError("defina HA018_ATHLETES_FILE, HA018_SLOTS_FILE y HA018_CONTENDED_SLOT")
    with _lock:
        index = next(_counter)
    athletes, slots = _csv(ATHLETES_FILE), _csv(SLOTS_FILE)
    if index >= min(len(athletes), DISTINCT + CONTENDED) or len(slots) < DISTINCT:
        raise RuntimeError("faltan cuentas o intervalos para la ráfaga")
    email, password = athletes[index][:2]
    if index < DISTINCT:
        service_id, availability_id = slots[index][:2]
        return index, (email, password), "distinct", (service_id, availability_id)
    service_id, availability_id = CONTENDED_SLOT.split(",")
    return index, (email, password), "contended", (service_id.strip(), availability_id.strip())


def _error_code(response: Any) -> str | None:
    try:
        code = response.json().get("code")
    except (ValueError, AttributeError):
        return None
    return str(code) if code is not None else None


class VirtualSessionAthlete(HttpUser):
    """Un deportista, una reserva; después se detiene."""

    wait_time = constant(0)

    def on_start(self) -> None:
        try:
            _, (email, password), self.burst, self.slot = _assignment()
            response = self.client.post(
                "/v1/sessions",
                name="setup: POST /v1/sessions",
                json={"email": email, "password": password},
            )
            response.raise_for_status()
            self.headers = {"Authorization": f"Bearer {response.json()['access_token']}"}
        except Exception as error:
            logging.getLogger("ha018").error("preparación fallida: %s", type(error).__name__)
            raise StopUser from error

    @task
    def book(self) -> None:
        service_id, availability_id = self.slot
        with self.client.post(
            APPOINTMENTS,
            name=f"POST {APPOINTMENTS}",
            json={"service_id": service_id, "availability_id": availability_id},
            headers={**self.headers, "Idempotency-Key": str(uuid.uuid4())},
            catch_response=True,
        ) as response:
            code = _error_code(response) if response.status_code != CREATED else None
            with _lock:
                _outcomes[(self.burst, response.status_code, code)] += 1
            if response.status_code == CREATED or (
                self.burst == "contended" and (response.status_code, code) == FULL
            ):
                response.success()
            else:
                response.failure(f"{response.status_code} {code}")
        raise StopUser


@events.quitting.add_listener
def _check_thresholds(environment: Environment, **_: Any) -> None:
    """Falla la corrida si no se cumple HA018 (latencia, errores y una sola confirmada)."""
    logger = logging.getLogger("ha018")
    stats = environment.stats.get(f"POST {APPOINTMENTS}", "POST")
    p95 = stats.get_response_time_percentile(0.95) or 0
    confirmed = {burst: _outcomes[(burst, CREATED, None)] for burst in ("distinct", "contended")}
    full = _outcomes[("contended", *FULL)]
    logger.info(
        "HA018: p50=%s p95=%s p99=%s ms, peticiones=%s, errores=%s, confirmadas=%s, llenas=%s",
        stats.get_response_time_percentile(0.5),
        p95,
        stats.get_response_time_percentile(0.99),
        stats.num_requests,
        stats.num_failures,
        confirmed,
        full,
    )
    failures = []
    if p95 >= P95_MS:
        failures.append(f"p95 {p95} ms ≥ {P95_MS} ms")
    if stats.num_failures:
        failures.append(f"{stats.num_failures} respuestas fuera del contrato")
    if confirmed["distinct"] != DISTINCT:
        failures.append(f"distinct: {confirmed['distinct']} de {DISTINCT} confirmadas")
    if (confirmed["contended"], full) != (1, CONTENDED - 1):
        failures.append(f"contended: {confirmed['contended']} confirmadas y {full} llenas")
    if failures:
        logger.error("HA018 no cumple: %s", "; ".join(failures))
        environment.process_exit_code = 1
