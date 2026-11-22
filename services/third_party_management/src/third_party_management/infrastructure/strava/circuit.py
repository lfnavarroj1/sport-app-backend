"""Circuito del proveedor (ADR-042 §9, HA013): se abre 60 s tras 5 fallos consecutivos.

Cuentan como fallo los `5xx`, los timeouts y los errores de conexión de cada intento
(también los reintentos). Abierto, no se llama al proveedor; al vencer los 60 s se deja
pasar la siguiente llamada: si falla se vuelve a abrir y si responde se cierra. Es por
proceso y seguro entre hilos (la API atiende en un *threadpool*).
"""

import threading
import time
from collections.abc import Callable

FAILURE_THRESHOLD = 5
OPEN_SECONDS = 60.0


class CircuitBreaker:
    def __init__(
        self,
        *,
        failure_threshold: int = FAILURE_THRESHOLD,
        open_seconds: float = OPEN_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._threshold = failure_threshold
        self._open_seconds = open_seconds
        self._clock = clock
        self._failures = 0
        self._opened_at: float | None = None
        self._lock = threading.Lock()

    @property
    def is_open(self) -> bool:
        with self._lock:
            return self._blocked()

    def _blocked(self) -> bool:
        return self._opened_at is not None and self._clock() - self._opened_at < self._open_seconds

    def allow(self) -> bool:
        with self._lock:
            return not self._blocked()

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._opened_at = None

    def record_failure(self) -> None:
        with self._lock:
            self._failures += 1
            if self._failures >= self._threshold:
                self._opened_at = self._clock()
