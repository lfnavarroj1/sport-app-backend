"""Cliente HTTP de `third_party_management` según su OpenAPI (HU031, ADR-042).

Reenvía el access token del deportista, la clave de idempotencia y la correlación; Terceros
vuelve a verificar identidad, actor y capacidad. El callback de Strava viaja sin token y con
los parámetros en un cuerpo JSON (nunca en la URL): `code`, `state` y `scope` no deben quedar
en ningún registro de solicitudes. Ningún token de Strava llega a este cliente.
"""

import httpx

from bff_mobile.clients.users_management import UpstreamTimeout, UpstreamUnavailable
from sportapp_platform.correlation import CORRELATION_HEADER, current_correlation_id

IDEMPOTENCY_HEADER = "Idempotency-Key"
STRAVA_INTEGRATION_PATH = "/v1/me/integrations/strava"


class ThirdPartyManagementClient:
    def __init__(self, http: httpx.Client) -> None:
        self._http = http

    def start_strava_authorization(self, access_token: str) -> httpx.Response:
        """HU031 CA1: crea el `state` y devuelve la URL de autorización de Strava."""
        return self._send("POST", f"{STRAVA_INTEGRATION_PATH}/authorization", access_token)

    def strava_integration(self, access_token: str) -> httpx.Response:
        """HU031 CA2: estado de la vinculación y de la última sincronización."""
        return self._send("GET", STRAVA_INTEGRATION_PATH, access_token)

    def unlink_strava(self, access_token: str) -> httpx.Response:
        """HU031 CA9: desvincular (idempotente); sin cuerpo ni clave."""
        return self._send("DELETE", STRAVA_INTEGRATION_PATH, access_token)

    def request_strava_sync(self, access_token: str, idempotency_key: str | None) -> httpx.Response:
        """HU031 CA4: solicitar una sincronización manual; la clave viaja tal cual (o ausente:
        la exige Terceros)."""
        return self._send(
            "POST",
            f"{STRAVA_INTEGRATION_PATH}/syncs",
            access_token,
            idempotency_key=idempotency_key,
        )

    def complete_strava_authorization(self, parameters: dict[str, str | None]) -> httpx.Response:
        """HU031 CA1/CA3: contrato interno del callback, sin token de SportApp.

        Los parámetros que Strava devolvió al navegador viajan en el cuerpo JSON, tal cual
        (`null` si faltaban): Terceros valida el `state`, el `scope` y el intercambio del `code`.
        """
        headers = _correlation()
        try:
            return self._http.post(
                "/v1/integrations/strava/callback", json=parameters, headers=headers
            )
        except httpx.TimeoutException:
            raise UpstreamTimeout() from None
        except httpx.TransportError:
            raise UpstreamUnavailable() from None

    def _send(
        self,
        method: str,
        path: str,
        access_token: str,
        *,
        idempotency_key: str | None = None,
    ) -> httpx.Response:
        """Las operaciones autenticadas no llevan cuerpo; los errores los responde Terceros."""
        headers = {"Authorization": f"Bearer {access_token}", **_correlation()}
        if idempotency_key is not None:
            headers[IDEMPOTENCY_HEADER] = idempotency_key
        try:
            if method == "GET":
                return self._http.get(path, headers=headers)
            if method == "DELETE":
                return self._http.delete(path, headers=headers)
            return self._http.post(path, headers=headers)
        except httpx.TimeoutException:
            raise UpstreamTimeout() from None
        except httpx.TransportError:
            raise UpstreamUnavailable() from None


def _correlation() -> dict[str, str]:
    correlation_id = current_correlation_id()
    return {CORRELATION_HEADER: correlation_id} if correlation_id else {}
