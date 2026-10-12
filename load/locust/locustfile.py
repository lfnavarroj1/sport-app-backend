"""Escenario técnico de referencia para Locust (ADR-014).

Uso (grupo de dependencias `load`, fuera de cada PR):
    uv run --group load locust -f load/locust/locustfile.py --host http://127.0.0.1:8000

El escenario de registro de usuarios (repetición de EXP-001 sobre FastAPI) se
agrega cuando exista la operación aprobada de HU001.
"""

from locust import HttpUser, between, task


class HealthUser(HttpUser):
    wait_time = between(0.5, 1.5)

    @task
    def live(self) -> None:
        self.client.get("/health/live")

    @task
    def ready(self) -> None:
        self.client.get("/health/ready")
