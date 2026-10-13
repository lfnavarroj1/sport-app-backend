"""HU032: `GET /v1/training-sessions/active` lee la sesión activa proyectada (CQRS).

Solo para `athlete` y siempre la propia; no consulta Afiliación (ADR-029). Puede ir por
detrás de la escritura: el cliente nunca la usa para contradecir una versión local más
reciente que aún se sincroniza.
"""

from training_management.application.access import require_athlete
from training_management.application.ports import ActiveSessionReader, ActiveSessionView, Caller


class GetActiveTrainingSession:
    def __init__(self, *, reader: ActiveSessionReader) -> None:
        self._reader = reader

    def __call__(self, caller: Caller) -> ActiveSessionView | None:
        require_athlete(caller)
        return self._reader.current(caller.user_id)
