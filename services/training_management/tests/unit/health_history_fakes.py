"""Dobles en memoria de HU012: `health_report_view` y autorizaciones proyectadas."""

from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID, uuid4

from training_management.application.ports import (
    AuthorizationView,
    HealthReportHistoryEntry,
    HealthReportHistoryQuery,
)


@dataclass
class InMemoryHealthHistoryStore:
    reports: dict[UUID, HealthReportHistoryEntry] = field(default_factory=dict)
    authorizations: dict[tuple[UUID, UUID], AuthorizationView] = field(default_factory=dict)

    def add(self, *entries: HealthReportHistoryEntry) -> None:
        for item in entries:
            self.reports[item.report_id] = item


class InMemoryHealthReportHistoryReader:
    """Mismo filtro, orden y keyset que `SqlAlchemyHealthReportHistoryReader`."""

    def __init__(self, store: InMemoryHealthHistoryStore) -> None:
        self.store = store

    def list(self, query: HealthReportHistoryQuery) -> list[HealthReportHistoryEntry]:
        def matches(item: HealthReportHistoryEntry) -> bool:
            return (
                item.athlete_id == query.athlete_id
                and (query.occurred_from is None or item.occurred_at >= query.occurred_from)
                and (query.occurred_before is None or item.occurred_at < query.occurred_before)
                and (query.type is None or item.type == query.type)
                and (query.severity is None or item.severity == query.severity)
                and (
                    query.after is None
                    or (item.occurred_at, item.report_id)
                    < (query.after.occurred_at, query.after.report_id)
                )
            )

        found = sorted(
            filter(matches, self.store.reports.values()),
            key=lambda item: (item.occurred_at, item.report_id),
            reverse=True,
        )
        return found[: query.limit]


class InMemoryHealthAuthorizationReader:
    def __init__(self, store: InMemoryHealthHistoryStore) -> None:
        self.store = store

    def get(self, athlete_id: UUID, professional_id: UUID) -> AuthorizationView | None:
        return self.store.authorizations.get((athlete_id, professional_id))


def report_entry(
    athlete_id: UUID,
    occurred_at: datetime,
    *,
    type: str = "DISCOMFORT",
    severity: str = "LOW",
    body_area: str = "KNEE",
    expected_recovery_days: int | None = 3,
    session_id: UUID | None = None,
) -> HealthReportHistoryEntry:
    return HealthReportHistoryEntry(
        report_id=uuid4(),
        athlete_id=athlete_id,
        type=type,
        body_area=body_area,
        severity=severity,
        occurred_at=occurred_at,
        expected_recovery_days=expected_recovery_days,
        status="OPEN",
        session_id=session_id,
    )


def authorization(
    athlete_id: UUID, professional_id: UUID, granted_at: datetime, *, active: bool = True
) -> AuthorizationView:
    return AuthorizationView(
        athlete_id=athlete_id,
        professional_id=professional_id,
        status="ACTIVE" if active else "REVOKED",
        version=1 if active else 2,
        granted_at=granted_at,
        revoked_at=None if active else granted_at,
    )
