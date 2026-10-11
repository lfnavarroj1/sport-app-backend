"""Línea base vacía del historial de única del dominio de `complementary_services`.

Fija la raíz del historial antes de la primera historia de usuario.

Revision ID: 0001_baseline
Revises:
Create Date: 2026-10-07
"""

from collections.abc import Sequence

revision: str = "0001_baseline"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
