"""Cliente HTTP de `users_management` según su OpenAPI 0.2.0."""

from typing import Any
from uuid import UUID

import httpx

from sportapp_platform.correlation import CORRELATION_HEADER, current_correlation_id

IDEMPOTENCY_HEADER = "Idempotency-Key"


class UpstreamUnavailable(Exception):
    code = "upstream_unavailable"


class UpstreamTimeout(Exception):
    code = "upstream_timeout"


class UsersManagementClient:
    def __init__(self, http: httpx.Client) -> None:
        self._http = http

    def register_user(self, payload: dict[str, Any], idempotency_key: UUID) -> httpx.Response:
        headers = {IDEMPOTENCY_HEADER: str(idempotency_key)}
        correlation_id = current_correlation_id()
        if correlation_id:
            headers[CORRELATION_HEADER] = correlation_id
        try:
            return self._http.post("/v1/users", json=payload, headers=headers)
        except httpx.TimeoutException:
            raise UpstreamTimeout() from None
        except httpx.TransportError:
            raise UpstreamUnavailable() from None
