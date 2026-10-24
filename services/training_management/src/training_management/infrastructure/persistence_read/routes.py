"""Base de lectura de HU026: filas de `route_view`, rutas del organizador y sugerencias."""

from datetime import datetime
from typing import Any

from sqlalchemy import ColumnElement, Select, and_, case, select, tuple_
from sqlalchemy.orm import Session, sessionmaker

from training_management.application.ports import MyRoutesQuery, RouteSuggestionQuery
from training_management.application.route_documents import route_from_document
from training_management.domain.routes.events import route_document
from training_management.domain.routes.model import Route, RouteStatus
from training_management.domain.routes.suggestions import city_key
from training_management.infrastructure.persistence_read.models import RouteViewRow


def route_view_values(route: Route, projected_at: datetime) -> dict[str, Any]:
    return {
        "route_id": route.route_id,
        "organizer_id": route.organizer_id,
        "status": route.status.value,
        "sport": route.sport.value,
        "city_key": city_key(route.city),
        "country_code": route.country_code,
        "distance_km": route.distance_km,
        "document": route_document(route),
        "version": route.version,
        "created_at": route.created_at,
        "published_at": route.published_at,
        "projected_at": projected_at,
    }


def suggestion_statement(query: RouteSuggestionQuery) -> Select[tuple[dict[str, Any]]]:
    row = RouteViewRow
    statement = select(row.document).where(row.status == RouteStatus.PUBLISHED.value)
    context = query.context
    if not context.personalized:
        # Sin deportes: las últimas publicadas, sin personalizar.
        return statement.order_by(row.published_at.desc(), row.route_id.desc()).limit(query.limit)
    statement = statement.where(row.sport.in_([sport.value for sport in context.sports]))
    if context.city_key is None:
        return statement.order_by(row.distance_km, row.route_id).limit(query.limit)
    city_match: ColumnElement[bool] = row.city_key == context.city_key
    if context.country_code is not None:
        city_match = and_(city_match, row.country_code == context.country_code)
    # Mismo orden que `domain.routes.suggestions.personalized_order`.
    return statement.order_by(case((city_match, 0), else_=1), row.distance_km, row.route_id).limit(
        query.limit
    )


class SqlAlchemyRouteReader:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def list_by_organizer(self, query: MyRoutesQuery) -> list[Route]:
        row = RouteViewRow
        statement = select(row.document).where(row.organizer_id == query.organizer_id)
        if query.after is not None:
            # Keyset: estrictamente después de la última entregada en el orden descendente.
            statement = statement.where(
                tuple_(row.created_at, row.route_id)
                < tuple_(query.after.created_at, query.after.route_id)
            )
        statement = statement.order_by(row.created_at.desc(), row.route_id.desc()).limit(
            query.limit
        )
        with self._session_factory() as session:
            return [route_from_document(document) for document in session.scalars(statement)]

    def suggest(self, query: RouteSuggestionQuery) -> list[Route]:
        with self._session_factory() as session:
            return [
                route_from_document(document)
                for document in session.scalars(suggestion_statement(query))
            ]
