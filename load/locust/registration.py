"""HA003: registro de usuarios a través del BFF web (repetición de EXP-001 sobre FastAPI).

Uso (fuera de cada PR; datos sintéticos):
    uv run --group load locust -f load/locust/registration.py \
        --host http://127.0.0.1:8000 --users 1000 --spawn-rate 100 --run-time 5m \
        --headless --csv reports/load/registration

REGISTRATION_POLICIES_VERSION debe coincidir con la configurada en el servicio.
El criterio es p95 < 3 s con 1.000 registros simultáneos y errores bajo el umbral acordado.
"""

import os
import uuid

from locust import HttpUser, between, task

POLICIES_VERSION = os.environ.get("REGISTRATION_POLICIES_VERSION", "local-dev")


class RegisteringUser(HttpUser):
    wait_time = between(0.5, 1.5)

    @task
    def register(self) -> None:
        unique = uuid.uuid4().hex
        self.client.post(
            "/v1/users",
            name="POST /v1/users",
            headers={"Idempotency-Key": str(uuid.uuid4())},
            json={
                "full_name": "Usuario Sintético",
                "email": f"carga-{unique}@example.test",
                "password": f"clave-sintetica-{unique}",
                "actor_type": "athlete",
                "accepted_policies_version": POLICIES_VERSION,
            },
        )
