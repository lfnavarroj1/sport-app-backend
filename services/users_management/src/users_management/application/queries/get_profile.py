"""HU005: `GET /v1/me/profile` sobre la proyección `athlete_profile_view` (ADR-034).

Consulta eventual: lee solo la base de lectura. Sin fila, todas las secciones son `null`;
si la base de lectura no responde, `ReadModelUnavailable` (→ `503`).
"""

from uuid import UUID

from users_management.application.ports import AthleteProfileReader, AthleteProfileView


class GetAthleteProfile:
    def __init__(self, reader: AthleteProfileReader) -> None:
        self._reader = reader

    def __call__(self, user_id: UUID) -> AthleteProfileView:
        view = self._reader.get(user_id)
        if view is None:
            return AthleteProfileView(
                user_id=user_id,
                demographic=None,
                physiological=None,
                sport_habits=None,
                demographic_version=None,
                physiological_version=None,
                sport_habits_version=None,
                projected_at=None,
            )
        return view
