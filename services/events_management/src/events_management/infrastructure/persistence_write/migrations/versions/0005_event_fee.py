"""HU025: tarifa informativa del evento (ADR-040).

- `event.fee_amount NUMERIC(12,2)`, `event.fee_currency` y `event.fee_updated_at`, todas
  opcionales: ningún evento existente tiene tarifa, así que no hay relleno.
- Restricciones: importe y moneda juntos, solo `COP`, importe entre 0 y 99999999.99, y una
  tarifa siempre con su instante de cambio.
- `command_idempotency` se reutiliza con `operation = 'put_event_fee'` y
  `'delete_event_fee'`; `event_registration.applied_fee_*` no cambia (ADR-032).

Solo expansión, compatible con la versión anterior del servicio. La reversión elimina las
restricciones y las columnas; se pierden las tarifas asignadas.

Revision ID: 0005_event_fee
Revises: 0004_event_publication
Create Date: 2026-10-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_event_fee"
down_revision: str | None = "0004_event_publication"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CONSTRAINTS = {
    "ck_event_fee": "(fee_amount IS NULL) = (fee_currency IS NULL)",
    "ck_event_fee_currency": "fee_currency = 'COP'",
    "ck_event_fee_amount": "fee_amount BETWEEN 0 AND 99999999.99",
    "ck_event_fee_updated_at": "fee_amount IS NULL OR fee_updated_at IS NOT NULL",
}


def upgrade() -> None:
    op.add_column("event", sa.Column("fee_amount", sa.Numeric(precision=12, scale=2)))
    op.add_column("event", sa.Column("fee_currency", sa.String(length=3)))
    op.add_column("event", sa.Column("fee_updated_at", sa.DateTime(timezone=True)))
    for name, condition in CONSTRAINTS.items():
        op.create_check_constraint(op.f(name), "event", condition)


def downgrade() -> None:
    for name in reversed(CONSTRAINTS):
        op.drop_constraint(op.f(name), "event", type_="check")
    op.drop_column("event", "fee_updated_at")
    op.drop_column("event", "fee_currency")
    op.drop_column("event", "fee_amount")
