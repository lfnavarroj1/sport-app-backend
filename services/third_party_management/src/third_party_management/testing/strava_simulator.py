"""Simulador de Strava para pruebas (HU031, ADR-042, HA013, EXP-002).

Nunca llama a Strava real: es un `httpx.MockTransport` programable que se inyecta en
`create_app(strava_http=...)`, en `StravaHttpGateway` o en el worker. Todos los datos son
sintéticos y los tokens se generan al azar en cada ejecución.

API estable (otras suites, como la prueba de componente de `bff-mobile`, la reutilizan):

```python
from third_party_management.testing.strava_simulator import (
    StravaSimulator, activity, rate_limited, server_error, status, timeout, unreadable,
)

strava = StravaSimulator(athlete_id=12345)
strava.activities = [activity(1001), activity(1002, sport_type="Ride", distance=25000.5)]
strava.queue("activities", server_error(), timeout("read"))  # dos fallas y luego normal
client = strava.client()        # httpx.Client con base_url https://www.strava.com
strava.calls("activities")      # solicitudes recibidas por operación
strava.sensitive_values()       # tokens emitidos, `code` y `client_secret` recibidos
```

Operaciones (`endpoint`):

- `"token"`: `POST /oauth/token` con `grant_type=authorization_code`. Por defecto entrega
  tokens nuevos, `expires_at` = ahora + `token_lifetime` y `athlete.id`.
- `"refresh"`: `POST /oauth/token` con `grant_type=refresh_token`; tokens nuevos. Con
  `strict_tokens` solo acepta el último `refresh_token` emitido (si no, `400`).
- `"deauthorize"`: `POST /oauth/deauthorize`; `200` e invalida el token.
- `"activities"`: `GET /api/v3/athlete/activities` paginado (`page`, `per_page`) y filtrado
  por `after` (epoch, estricto) sobre `activities`. Cada respuesta lleva
  `X-RateLimit-Limit`/`X-RateLimit-Usage` (`rate_limit`, `usage`, que se incrementa). Con
  `strict_tokens` solo acepta los access tokens vigentes emitidos (si no, `401`);
  `expire_access_tokens()` invalida los actuales para provocar la renovación.

Escenarios encolados con `queue(endpoint, *outcomes)`; se consumen en orden y, agotados,
vuelve el comportamiento por defecto:

- `ok()`: comportamiento por defecto.
- `status(code, json=..., headers=...)`: respuesta fija (p. ej. `400`, `401`, `403`, `404`).
- `rate_limited(retry_after=..., usage=..., limit=...)`: `429` con `Retry-After` y cuota.
- `server_error(code=503)`: `5xx`.
- `timeout("connect" | "read")`: `httpx.ConnectTimeout` / `httpx.ReadTimeout`.
- `connection_error()`: `httpx.ConnectError`.
- `unreadable()`: `200` con un cuerpo que no es JSON.
- `page(records)`: `200` con esa lista exacta (actividades parciales, duplicadas o
  cambiadas en una página concreta).

`activity(id, ...)` construye una actividad válida de Strava; `omit=("elapsed_time",)` la
deja parcial y cualquier campo se puede sobrescribir.
"""

import secrets
from collections import defaultdict, deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs

import httpx

BASE_URL = "https://www.strava.com"
CODE_EXCHANGE = "token"
RENEWAL = "refresh"
DEAUTHORIZE = "deauthorize"
ACTIVITIES = "activities"
ENDPOINTS = (CODE_EXCHANGE, RENEWAL, DEAUTHORIZE, ACTIVITIES)
DEFAULT_ATHLETE_ID = 12345
DEFAULT_PER_PAGE = 30
OK = 200
BAD_REQUEST = 400
UNAUTHORIZED = 401
NOT_FOUND = 404
TOO_MANY_REQUESTS = 429


@dataclass(frozen=True)
class Outcome:
    """Resultado programado para una solicitud. `status=None` = comportamiento por defecto."""

    status: int | None = None
    body: Any = None
    headers: dict[str, str] = field(default_factory=dict)
    raw: bytes | None = None
    error: type[httpx.TransportError] | None = None


def ok() -> Outcome:
    return Outcome()


def status(code: int, *, json: Any = None, headers: dict[str, str] | None = None) -> Outcome:
    body = json if json is not None else {"message": "Simulated", "errors": []}
    return Outcome(status=code, body=body, headers=headers or {})


def rate_limited(
    retry_after: int = 900, usage: str = "100,1000", limit: str = "100,1000"
) -> Outcome:
    return Outcome(
        status=TOO_MANY_REQUESTS,
        body={"message": "Rate Limit Exceeded", "errors": []},
        headers={
            "Retry-After": str(retry_after),
            "X-RateLimit-Usage": usage,
            "X-RateLimit-Limit": limit,
        },
    )


def server_error(code: int = 503) -> Outcome:
    return Outcome(status=code, body={"message": "Simulated outage", "errors": []})


def timeout(kind: str = "read") -> Outcome:
    return Outcome(error=httpx.ConnectTimeout if kind == "connect" else httpx.ReadTimeout)


def connection_error() -> Outcome:
    return Outcome(error=httpx.ConnectError)


def unreadable() -> Outcome:
    return Outcome(status=OK, raw=b"<html>Simulated</html>")


def page(records: list[Any]) -> Outcome:
    return Outcome(status=OK, body=records)


def activity(
    activity_id: int,
    *,
    start_date: str = "2026-10-20T11:00:00Z",
    sport_type: str = "Run",
    elapsed_time: int = 3600,
    distance: float = 10000.4,
    omit: Iterable[str] = (),
    **overrides: Any,
) -> dict[str, Any]:
    """Actividad sintética con los campos de Strava que usa `strava-activity.v1`."""
    record: dict[str, Any] = {
        "id": activity_id,
        "name": f"Actividad sintética {activity_id}",
        "type": sport_type,
        "sport_type": sport_type,
        "start_date": start_date,
        "start_date_local": start_date.replace("Z", ""),
        "timezone": "(GMT-05:00) America/Bogota",
        "elapsed_time": elapsed_time,
        "moving_time": max(elapsed_time - 120, 0),
        "distance": distance,
        "total_elevation_gain": 85.5,
        "athlete": {"id": DEFAULT_ATHLETE_ID},
    }
    record.update(overrides)
    for name in omit:
        record.pop(name, None)
    return record


def _form(request: httpx.Request) -> dict[str, str]:
    parsed = parse_qs(request.content.decode("utf-8"))
    return {name: values[0] for name, values in parsed.items()}


def _bearer(request: httpx.Request) -> str:
    header = request.headers.get("Authorization", "")
    return header[len("Bearer ") :] if header.startswith("Bearer ") else ""


def _epoch_of(value: object) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return None


class StravaSimulator:
    def __init__(
        self,
        *,
        athlete_id: int = DEFAULT_ATHLETE_ID,
        token_lifetime: timedelta = timedelta(hours=6),
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        strict_tokens: bool = False,
    ) -> None:
        self.athlete_id = athlete_id
        self.token_lifetime = token_lifetime
        self.now = now
        self.strict_tokens = strict_tokens
        self.activities: list[Any] = []
        self.rate_limit: tuple[int, int] = (200, 2000)
        self.usage: list[int] = [0, 0]
        self.requests: list[httpx.Request] = []
        self._calls: dict[str, list[httpx.Request]] = defaultdict(list)
        self._queues: dict[str, deque[Outcome]] = defaultdict(deque)
        self._issued: list[tuple[str, str]] = []
        self._valid_access: set[str] = set()
        self._current_refresh: str | None = None
        self._received: list[str] = []

    # ------------------------------------------------------------- programación

    def queue(self, endpoint: str, *outcomes: Outcome) -> None:
        if endpoint not in ENDPOINTS:
            raise ValueError(f"Operación desconocida: {endpoint}")
        self._queues[endpoint].extend(outcomes)

    def calls(self, endpoint: str) -> list[httpx.Request]:
        return list(self._calls[endpoint])

    def expire_access_tokens(self) -> None:
        self.strict_tokens = True
        self._valid_access.clear()

    def issue_tokens(self) -> tuple[str, str]:
        """Emite un par (access, refresh) como si viniera de un intercambio previo."""
        access, refresh = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
        self._issued.append((access, refresh))
        self._valid_access.add(access)
        self._current_refresh = refresh
        return access, refresh

    @property
    def issued_tokens(self) -> list[tuple[str, str]]:
        return list(self._issued)

    def sensitive_values(self) -> list[str]:
        """Todo lo que nunca debe aparecer en logs, eventos ni respuestas públicas."""
        tokens = [value for pair in self._issued for value in pair]
        return [*tokens, *self._received]

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def client(self, base_url: str = BASE_URL) -> httpx.Client:
        return httpx.Client(transport=self.transport, base_url=base_url)

    # ---------------------------------------------------------------- manejo

    def _endpoint(self, request: httpx.Request) -> str | None:
        path = request.url.path
        if request.method == "POST" and path == "/oauth/token":
            grant = _form(request).get("grant_type")
            return RENEWAL if grant == "refresh_token" else CODE_EXCHANGE
        if request.method == "POST" and path == "/oauth/deauthorize":
            return DEAUTHORIZE
        if request.method == "GET" and path == "/api/v3/athlete/activities":
            return ACTIVITIES
        return None

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        endpoint = self._endpoint(request)
        if endpoint is None:
            return httpx.Response(NOT_FOUND, json={"message": "Record Not Found"})
        self._calls[endpoint].append(request)
        self._remember_secrets(request)
        queued = self._queues[endpoint]
        outcome = queued.popleft() if queued else Outcome()
        if outcome.error is not None:
            raise outcome.error("Simulated", request=request)
        if outcome.raw is not None:
            return httpx.Response(outcome.status or OK, content=outcome.raw)
        if outcome.status is not None:
            headers = dict(outcome.headers)
            if endpoint == ACTIVITIES:
                headers = {**self._rate_headers(), **headers}
            return httpx.Response(outcome.status, json=outcome.body, headers=headers)
        handlers = {
            CODE_EXCHANGE: self._token,
            RENEWAL: self._refresh,
            DEAUTHORIZE: self._deauthorize,
            ACTIVITIES: self._activities,
        }
        return handlers[endpoint](request)

    def _remember_secrets(self, request: httpx.Request) -> None:
        if request.method != "POST":
            return
        form = _form(request)
        for name in ("code", "client_secret", "refresh_token", "access_token"):
            if form.get(name):
                self._received.append(form[name])

    def _token_document(self, *, with_athlete: bool) -> dict[str, Any]:
        access, refresh = self.issue_tokens()
        expires_at = self.now() + self.token_lifetime
        document: dict[str, Any] = {
            "token_type": "Bearer",
            "expires_at": int(expires_at.timestamp()),
            "expires_in": int(self.token_lifetime.total_seconds()),
            "refresh_token": refresh,
            "access_token": access,
        }
        if with_athlete:
            document["athlete"] = {"id": self.athlete_id, "firstname": "Sintético"}
        return document

    def _token(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(OK, json=self._token_document(with_athlete=True))

    def _refresh(self, request: httpx.Request) -> httpx.Response:
        presented = _form(request).get("refresh_token")
        if self.strict_tokens and presented != self._current_refresh:
            return httpx.Response(BAD_REQUEST, json={"message": "Bad Request", "errors": []})
        return httpx.Response(OK, json=self._token_document(with_athlete=False))

    def _deauthorize(self, request: httpx.Request) -> httpx.Response:
        presented = _form(request).get("access_token", "")
        self._valid_access.discard(presented)
        return httpx.Response(OK, json={"access_token": presented})

    def _rate_headers(self) -> dict[str, str]:
        return {
            "X-RateLimit-Limit": f"{self.rate_limit[0]},{self.rate_limit[1]}",
            "X-RateLimit-Usage": f"{self.usage[0]},{self.usage[1]}",
        }

    def _activities(self, request: httpx.Request) -> httpx.Response:
        if self.strict_tokens and _bearer(request) not in self._valid_access:
            return httpx.Response(
                UNAUTHORIZED, json={"message": "Authorization Error", "errors": []}
            )
        params = request.url.params
        number = int(params.get("page", "1"))
        per_page = int(params.get("per_page", str(DEFAULT_PER_PAGE)))
        after = params.get("after")
        records = self.activities
        if after is not None:
            threshold = float(after)
            records = [
                record
                for record in records
                if not isinstance(record, dict)
                or (_epoch_of(record.get("start_date")) or threshold + 1) > threshold
            ]
        chunk = records[(number - 1) * per_page : number * per_page]
        self.usage = [self.usage[0] + 1, self.usage[1] + 1]
        return httpx.Response(OK, json=chunk, headers=self._rate_headers())


def body_of(request: httpx.Request) -> dict[str, str]:
    """Formulario de una solicitud registrada (para aserciones)."""
    return _form(request)
