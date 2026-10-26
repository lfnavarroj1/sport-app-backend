"""Cliente HTTPX de Strava detrás del puerto neutral `StravaGateway` (ADR-007, ADR-042 §9).

- Timeout de conexión 2 s y de lectura 5 s (`STRAVA_TIMEOUT`); un cliente con pool por
  proceso.
- `5xx`, timeout o error de conexión → hasta 2 reintentos con espera exponencial y jitter
  (inyectables) en las operaciones seguras: listar actividades y renovar el token. La
  revocación hace un solo reintento (mejor esfuerzo). El intercambio del `code` no se
  reintenta: el `code` es de un solo uso (R).
- `429` → `RATE_LIMITED` sin reintento, con `Retry-After`; se registran `Retry-After`,
  `X-RateLimit-Usage` y `X-RateLimit-Limit`. Si tras una página la cuota de alguna ventana
  quedó agotada, `ActivityPage.quota_exhausted` detiene la corrida.
- `401` → `UNAUTHORIZED`; `403` → `FORBIDDEN`; otro `4xx` → `REJECTED`; cuerpo ilegible →
  `INVALID_RESPONSE`.
- Circuito abierto 60 s tras 5 fallos consecutivos: no se llama al proveedor
  (`CIRCUIT_OPEN`).

Nunca se registran URLs con parámetros, cuerpos, tokens, `code` ni `client_secret`.
"""

import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode

import httpx

from third_party_management.application.errors import ProviderFailure, ProviderFailureKind
from third_party_management.application.ports import (
    ActivityPage,
    ProviderGrant,
    ProviderTokens,
)
from third_party_management.domain.connections import REQUESTED_SCOPES
from third_party_management.infrastructure.strava.circuit import CircuitBreaker

STRAVA_TIMEOUT = httpx.Timeout(5.0, connect=2.0)
DEFAULT_AUTHORIZE_URL = "https://www.strava.com/oauth/mobile/authorize"
DEFAULT_BASE_URL = "https://www.strava.com"
TOKEN_PATH = "/oauth/token"  # noqa: S105  # nosec B105 - ruta, no secreto
DEAUTHORIZE_PATH = "/oauth/deauthorize"
ACTIVITIES_PATH = "/api/v3/athlete/activities"
SAFE_RETRIES = 2
DEAUTHORIZE_RETRIES = 1
BACKOFF_BASE_SECONDS = 0.5
# Pares (límite, uso) por ventana; Strava publica también los de lectura.
RATE_LIMIT_HEADERS = (
    ("X-RateLimit-Limit", "X-RateLimit-Usage"),
    ("X-ReadRateLimit-Limit", "X-ReadRateLimit-Usage"),
)

_logger = logging.getLogger("sportapp.strava")

Sleep = Callable[[float], None]
Jitter = Callable[[float, float], float]


@dataclass(frozen=True)
class StravaCredentials:
    client_id: str
    client_secret: str = field(repr=False)
    redirect_uri: str
    authorize_url: str = DEFAULT_AUTHORIZE_URL


def _jitter(low: float, high: float) -> float:
    # Dispersión de reintentos; no tiene uso criptográfico.
    return random.uniform(low, high)  # noqa: S311  # nosec B311


def _failure(kind: ProviderFailureKind, retry_after: int | None = None) -> ProviderFailure:
    return ProviderFailure(kind, retry_after)


def _retry_after(response: httpx.Response) -> int | None:
    value = response.headers.get("Retry-After", "")
    return int(value) if value.strip().isdigit() else None


def _windows(value: str | None) -> list[int] | None:
    if not value:
        return None
    try:
        return [int(part.strip()) for part in value.split(",")]
    except ValueError:
        return None


def quota_exhausted(headers: httpx.Headers) -> bool:
    """`True` si en alguna ventana el uso alcanzó el límite (`X-RateLimit-*`)."""
    for limit_header, usage_header in RATE_LIMIT_HEADERS:
        limits, usages = _windows(headers.get(limit_header)), _windows(headers.get(usage_header))
        if (
            limits
            and usages
            and any(
                usage >= limit for limit, usage in zip(limits, usages, strict=False) if limit > 0
            )
        ):
            return True
    return False


def _text(document: dict[str, Any], name: str) -> str:
    value = document.get(name)
    if not isinstance(value, str) or not value:
        raise _failure(ProviderFailureKind.INVALID_RESPONSE)
    return value


def _epoch(document: dict[str, Any], name: str) -> datetime:
    value = document.get(name)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise _failure(ProviderFailureKind.INVALID_RESPONSE)
    return datetime.fromtimestamp(value, UTC)


def _tokens(document: dict[str, Any]) -> ProviderTokens:
    return ProviderTokens(
        access_token=_text(document, "access_token"),
        refresh_token=_text(document, "refresh_token"),
        expires_at=_epoch(document, "expires_at"),
    )


class StravaHttpGateway:
    def __init__(
        self,
        http: httpx.Client,
        credentials: StravaCredentials,
        *,
        breaker: CircuitBreaker | None = None,
        sleep: Sleep = time.sleep,
        jitter: Jitter = _jitter,
        backoff_base_seconds: float = BACKOFF_BASE_SECONDS,
    ) -> None:
        self._http = http
        self._credentials = credentials
        self._breaker = breaker or CircuitBreaker()
        self._sleep = sleep
        self._jitter = jitter
        self._backoff = backoff_base_seconds

    # ----------------------------------------------------------- operaciones

    def authorization_url(self, state: str) -> str:
        query = urlencode(
            {
                "client_id": self._credentials.client_id,
                "redirect_uri": self._credentials.redirect_uri,
                "response_type": "code",
                "approval_prompt": "auto",
                "scope": ",".join(REQUESTED_SCOPES),
                "state": state,
            }
        )
        return f"{self._credentials.authorize_url}?{query}"

    def exchange_code(self, code: str) -> ProviderGrant:
        document = self._json_object(
            "exchange_code",
            0,
            lambda: self._http.post(
                TOKEN_PATH,
                data={
                    "client_id": self._credentials.client_id,
                    "client_secret": self._credentials.client_secret,
                    "code": code,
                    "grant_type": "authorization_code",
                },
            ),
        )
        athlete = document.get("athlete")
        athlete_id = athlete.get("id") if isinstance(athlete, dict) else None
        if not isinstance(athlete_id, int) or isinstance(athlete_id, bool) or athlete_id <= 0:
            raise _failure(ProviderFailureKind.INVALID_RESPONSE)
        return ProviderGrant(tokens=_tokens(document), external_athlete_id=str(athlete_id))

    def refresh(self, refresh_token: str) -> ProviderTokens:
        document = self._json_object(
            "refresh_token",
            SAFE_RETRIES,
            lambda: self._http.post(
                TOKEN_PATH,
                data={
                    "client_id": self._credentials.client_id,
                    "client_secret": self._credentials.client_secret,
                    "refresh_token": refresh_token,
                    "grant_type": "refresh_token",
                },
            ),
        )
        return _tokens(document)

    def deauthorize(self, access_token: str) -> None:
        response = self._send(
            "deauthorize",
            DEAUTHORIZE_RETRIES,
            lambda: self._http.post(DEAUTHORIZE_PATH, data={"access_token": access_token}),
        )
        self._raise_for_status("deauthorize", response)

    def list_activities(
        self, access_token: str, *, after: datetime | None, page: int, per_page: int
    ) -> ActivityPage:
        params: dict[str, int] = {"per_page": per_page, "page": page}
        if after is not None:
            params["after"] = int(after.timestamp())
        response = self._send(
            "list_activities",
            SAFE_RETRIES,
            lambda: self._http.get(
                ACTIVITIES_PATH,
                params=params,
                headers={"Authorization": f"Bearer {access_token}"},
            ),
        )
        self._raise_for_status("list_activities", response)
        try:
            document = response.json()
        except ValueError:
            raise _failure(ProviderFailureKind.INVALID_RESPONSE) from None
        if not isinstance(document, list):
            raise _failure(ProviderFailureKind.INVALID_RESPONSE)
        return ActivityPage(
            records=tuple(document), quota_exhausted=quota_exhausted(response.headers)
        )

    # -------------------------------------------------------------- técnica

    def _json_object(
        self, operation: str, retries: int, send: Callable[[], httpx.Response]
    ) -> dict[str, Any]:
        response = self._send(operation, retries, send)
        self._raise_for_status(operation, response)
        try:
            document = response.json()
        except ValueError:
            raise _failure(ProviderFailureKind.INVALID_RESPONSE) from None
        if not isinstance(document, dict):
            raise _failure(ProviderFailureKind.INVALID_RESPONSE)
        return document

    def _send(
        self, operation: str, retries: int, send: Callable[[], httpx.Response]
    ) -> httpx.Response:
        """Envía con reintentos ante `5xx`/timeout/conexión y respeta el circuito."""
        for attempt in range(retries + 1):
            if not self._breaker.allow():
                _logger.warning("strava.circuit_open", extra={"fields": {"operation": operation}})
                raise _failure(ProviderFailureKind.CIRCUIT_OPEN)
            try:
                response = send()
            except httpx.TransportError as error:
                self._breaker.record_failure()
                self._log_failure(operation, attempt, error_type=type(error).__name__)
            else:
                if response.status_code < httpx.codes.INTERNAL_SERVER_ERROR:
                    self._breaker.record_success()
                    return response
                self._breaker.record_failure()
                self._log_failure(operation, attempt, status_code=response.status_code)
            if attempt < retries:
                self._sleep(self._delay(attempt))
        raise _failure(ProviderFailureKind.UNAVAILABLE)

    def _delay(self, attempt: int) -> float:
        base: float = self._backoff * 2.0**attempt
        return base + self._jitter(0.0, self._backoff)

    @staticmethod
    def _log_failure(operation: str, attempt: int, **fields: object) -> None:
        _logger.warning(
            "strava.request_failed",
            extra={"fields": {"operation": operation, "attempt": attempt + 1, **fields}},
        )

    @staticmethod
    def _raise_for_status(operation: str, response: httpx.Response) -> None:
        status = response.status_code
        if httpx.codes.is_success(status):
            return
        if status == httpx.codes.TOO_MANY_REQUESTS:
            retry_after = _retry_after(response)
            _logger.warning(
                "strava.rate_limited",
                extra={
                    "fields": {
                        "operation": operation,
                        "retry_after_seconds": retry_after,
                        "rate_limit_usage": response.headers.get("X-RateLimit-Usage"),
                        "rate_limit_limit": response.headers.get("X-RateLimit-Limit"),
                    }
                },
            )
            raise _failure(ProviderFailureKind.RATE_LIMITED, retry_after)
        _logger.warning(
            "strava.request_rejected",
            extra={"fields": {"operation": operation, "status_code": status}},
        )
        raise _failure(_KIND_BY_STATUS.get(status, ProviderFailureKind.REJECTED))


_KIND_BY_STATUS: dict[int, ProviderFailureKind] = {
    httpx.codes.UNAUTHORIZED.value: ProviderFailureKind.UNAUTHORIZED,
    httpx.codes.FORBIDDEN.value: ProviderFailureKind.FORBIDDEN,
}
