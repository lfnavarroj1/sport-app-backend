"""Hash de contraseñas con Argon2id (ADR-009).

Parámetros recomendados por OWASP: memoria 19 MiB, 2 iteraciones, paralelismo 1.
Quedan registrados en cada hash (`$argon2id$v=19$m=19456,t=2,p=1$...`), lo que
permite verificar hashes antiguos y rehashear cuando se aumenten.
"""

from argon2 import PasswordHasher, Type

TIME_COST = 2
MEMORY_COST_KIB = 19 * 1024
PARALLELISM = 1


class Argon2idPasswordHasher:
    def __init__(self) -> None:
        self._hasher = PasswordHasher(
            time_cost=TIME_COST,
            memory_cost=MEMORY_COST_KIB,
            parallelism=PARALLELISM,
            hash_len=32,
            salt_len=16,
            type=Type.ID,
        )

    def hash(self, password: str) -> str:
        return self._hasher.hash(password)
