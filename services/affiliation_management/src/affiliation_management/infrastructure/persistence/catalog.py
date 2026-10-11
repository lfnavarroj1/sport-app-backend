"""Lectura del catálogo: capacidades vigentes de un usuario y sonda de disponibilidad."""

import logging
from uuid import UUID

from sqlalchemy import Engine, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from affiliation_management.application.ports import CapabilitySnapshot
from affiliation_management.domain.catalog import CAPABILITIES_BY_PLAN
from affiliation_management.domain.subscriptions.model import PlanVersionRef, SubscriptionStatus
from affiliation_management.infrastructure.persistence.models import (
    CapabilityRow,
    CatalogMetadataRow,
    PlanRow,
    PlanVersionCapabilityRow,
    PlanVersionRow,
    SubscriptionRow,
)

_logger = logging.getLogger("sportapp.catalog")


class SqlAlchemyCapabilitiesReader:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def active_snapshot(self, user_id: UUID) -> CapabilitySnapshot | None:
        with self._session_factory() as session:
            subscription = session.execute(
                select(
                    SubscriptionRow.plan_code,
                    SubscriptionRow.plan_version,
                    PlanVersionRow.catalog_version,
                )
                .join(
                    PlanVersionRow,
                    (PlanVersionRow.plan_code == SubscriptionRow.plan_code)
                    & (PlanVersionRow.version == SubscriptionRow.plan_version),
                )
                .where(
                    SubscriptionRow.user_id == user_id,
                    SubscriptionRow.status == SubscriptionStatus.ACTIVE.value,
                )
            ).one_or_none()
            if subscription is None:
                return None
            plan_code, plan_version, catalog_version = subscription
            capabilities = session.scalars(
                select(PlanVersionCapabilityRow.capability_code).where(
                    PlanVersionCapabilityRow.plan_code == plan_code,
                    PlanVersionCapabilityRow.plan_version == plan_version,
                )
            ).all()
        return CapabilitySnapshot(
            plan=PlanVersionRef(
                plan_code=plan_code, version=plan_version, catalog_version=catalog_version
            ),
            capabilities=tuple(capabilities),
        )


class CatalogReadinessCheck:
    """Afiliación no queda lista con un catálogo ausente, parcial o de otra versión (ADR-024).

    Exige la versión esperada en `catalog_metadata`, los tres planes y, para esa
    versión, exactamente las capacidades materializadas de cada plan.
    """

    name = "catalog"

    def __init__(self, engine: Engine, expected_version: str) -> None:
        self._engine = engine
        self._expected_version = expected_version

    def __call__(self) -> bool:
        try:
            problem = self._find_problem()
        except SQLAlchemyError as error:
            problem = f"database_error:{type(error).__name__}"
        if problem is None:
            return True
        _logger.warning(
            "catalog.not_ready",
            extra={"fields": {"expected_version": self._expected_version, "problem": problem}},
        )
        return False

    def _find_problem(self) -> str | None:
        with self._engine.connect() as connection:
            installed = connection.scalar(
                select(CatalogMetadataRow.catalog_version).where(
                    CatalogMetadataRow.catalog_version == self._expected_version
                )
            )
            if installed is None:
                return "catalog_version_missing"
            plans = set(connection.scalars(select(PlanRow.code)))
            if not set(CAPABILITIES_BY_PLAN) <= plans:
                return "plan_missing"
            known = set(connection.scalars(select(CapabilityRow.code)))
            required = {code for codes in CAPABILITIES_BY_PLAN.values() for code in codes}
            if not required <= known:
                return "capability_missing"
            materialized = connection.execute(
                select(PlanVersionCapabilityRow.plan_code, PlanVersionCapabilityRow.capability_code)
                .join(
                    PlanVersionRow,
                    (PlanVersionRow.plan_code == PlanVersionCapabilityRow.plan_code)
                    & (PlanVersionRow.version == PlanVersionCapabilityRow.plan_version),
                )
                .where(PlanVersionRow.catalog_version == self._expected_version)
            ).all()
        found: dict[str, set[str]] = {}
        for plan_code, capability_code in materialized:
            found.setdefault(plan_code, set()).add(capability_code)
        for plan_code, codes in CAPABILITIES_BY_PLAN.items():
            if found.get(plan_code) != set(codes):
                return f"plan_capabilities_mismatch:{plan_code}"
        return None
