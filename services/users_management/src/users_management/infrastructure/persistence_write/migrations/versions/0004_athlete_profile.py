"""HU005 (ADR-034): secciones demográfica y fisiológica del perfil del deportista.

Solo expansión (tablas nuevas): compatible con la versión anterior del código. Cada
tabla tiene una fila vigente por `user_id` (PK y FK local a `user_account`), versión
propia y restricciones de rango equivalentes al contrato. Los decimales usan
`NUMERIC(4,1)`. La reversión elimina las tablas: borra los perfiles registrados.

Revision ID: 0004_athlete_profile
Revises: 0003_authenticate_user
Create Date: 2026-10-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_athlete_profile"
down_revision: str | None = "0003_authenticate_user"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "demographic_profile",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column("birth_date", sa.Date(), nullable=False),
        sa.Column("gender", sa.String(length=32), nullable=False),
        sa.Column("country_code", sa.String(length=2), nullable=False),
        sa.Column("city", sa.String(length=80), nullable=False),
        sa.Column("marital_status", sa.String(length=32), nullable=True),
        sa.Column("occupation", sa.String(length=80), nullable=True),
        sa.Column("income_range", sa.String(length=32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("version >= 1", name=op.f("ck_demographic_profile_version")),
        sa.CheckConstraint(
            "gender IN ('FEMALE', 'MALE', 'NON_BINARY', 'PREFER_NOT_TO_SAY')",
            name=op.f("ck_demographic_profile_gender"),
        ),
        sa.CheckConstraint(
            "country_code ~ '^[A-Z]{2}$'", name=op.f("ck_demographic_profile_country_code")
        ),
        sa.CheckConstraint(
            "char_length(city) BETWEEN 2 AND 80", name=op.f("ck_demographic_profile_city")
        ),
        sa.CheckConstraint(
            "marital_status IS NULL OR marital_status IN ('SINGLE', 'MARRIED', "
            "'DOMESTIC_PARTNERSHIP', 'DIVORCED', 'WIDOWED', 'PREFER_NOT_TO_SAY')",
            name=op.f("ck_demographic_profile_marital_status"),
        ),
        sa.CheckConstraint(
            "occupation IS NULL OR char_length(occupation) BETWEEN 2 AND 80",
            name=op.f("ck_demographic_profile_occupation"),
        ),
        sa.CheckConstraint(
            "income_range IS NULL OR income_range IN ('LT_1_SMMLV', '1_2_SMMLV', "
            "'2_4_SMMLV', 'GT_4_SMMLV', 'PREFER_NOT_TO_SAY')",
            name=op.f("ck_demographic_profile_income_range"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["user_account.user_id"],
            name=op.f("fk_demographic_profile_user_id_user_account"),
        ),
        sa.PrimaryKeyConstraint("user_id", name=op.f("pk_demographic_profile")),
    )
    op.create_table(
        "physiological_profile",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column("health_consent_version", sa.String(length=64), nullable=False),
        sa.Column("health_consent_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("measured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("weight_kg", sa.Numeric(precision=4, scale=1), nullable=False),
        sa.Column("height_cm", sa.Integer(), nullable=False),
        sa.Column("bmi", sa.Numeric(precision=4, scale=1), nullable=False),
        sa.Column("resting_heart_rate_bpm", sa.Integer(), nullable=True),
        sa.Column("systolic_mmhg", sa.Integer(), nullable=True),
        sa.Column("diastolic_mmhg", sa.Integer(), nullable=True),
        sa.Column("body_temperature_c", sa.Numeric(precision=4, scale=1), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("version >= 1", name=op.f("ck_physiological_profile_version")),
        sa.CheckConstraint(
            "weight_kg BETWEEN 20.0 AND 300.0", name=op.f("ck_physiological_profile_weight_kg")
        ),
        sa.CheckConstraint(
            "height_cm BETWEEN 100 AND 250", name=op.f("ck_physiological_profile_height_cm")
        ),
        sa.CheckConstraint("bmi > 0", name=op.f("ck_physiological_profile_bmi")),
        sa.CheckConstraint(
            "resting_heart_rate_bpm IS NULL OR resting_heart_rate_bpm BETWEEN 30 AND 220",
            name=op.f("ck_physiological_profile_resting_heart_rate"),
        ),
        sa.CheckConstraint(
            # Ambas o ninguna; un `NULL` suelto haría pasar la restricción sin evaluarla.
            "(systolic_mmhg IS NULL) = (diastolic_mmhg IS NULL) AND (systolic_mmhg IS NULL "
            "OR (systolic_mmhg BETWEEN 60 AND 250 AND diastolic_mmhg BETWEEN 30 AND 150 "
            "AND systolic_mmhg > diastolic_mmhg))",
            name=op.f("ck_physiological_profile_blood_pressure"),
        ),
        sa.CheckConstraint(
            "body_temperature_c IS NULL OR body_temperature_c BETWEEN 34.0 AND 42.0",
            name=op.f("ck_physiological_profile_body_temperature"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["user_account.user_id"],
            name=op.f("fk_physiological_profile_user_id_user_account"),
        ),
        sa.PrimaryKeyConstraint("user_id", name=op.f("pk_physiological_profile")),
    )


def downgrade() -> None:
    op.drop_table("physiological_profile")
    op.drop_table("demographic_profile")
