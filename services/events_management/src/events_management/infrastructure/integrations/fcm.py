"""Envío opcional por Firebase Cloud Messaging HTTP v1 (HU039, ADR-045 §2 y §10).

Solo se construye con `FCM_ENABLED=true`. `POST /v1/projects/{FCM_PROJECT_ID}/messages:send`
con un mensaje de **datos** (sin bloque `notification`: la app decide canal y prioridad) y
un access token OAuth 2.0 de la cuenta de servicio `FCM_SERVICE_ACCOUNT` (alcance
`firebase.messaging`). `FCM_BASE_URL` permite apuntar a un simulador en pruebas.

Clasificación de respuestas (reintento acotado en `DeliverPush`):
- `200` → entregado a FCM;
- `429`, `5xx`, timeout o error de red → `PushFailed` (transitorio, con `Retry-After`);
- cualquier otro estado (p. ej. `400`, `403`, `404` token no registrado) → `PushRejected`;
- un fallo al obtener el access token de la cuenta de servicio → `PushFailed`
  (`credential_error`), para que quede registrado.
La prioridad de Android sigue la de la notificación (`HIGH` o `NORMAL`).
El token del dispositivo y la credencial nunca se registran.
"""

from collections.abc import Callable
from pathlib import Path

import httpx
from google.auth.transport.requests import Request
from google.oauth2 import service_account

from events_management.application.ports import PushFailed, PushRejected

SCOPE = "https://www.googleapis.com/auth/firebase.messaging"
TRANSIENT = frozenset({429, 500, 502, 503, 504})


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("Retry-After")
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None


class FcmPushSender:
    def __init__(
        self, http: httpx.Client, *, project_id: str, access_token: Callable[[], str]
    ) -> None:
        self._http = http
        self._path = f"/v1/projects/{project_id}/messages:send"
        self._access_token = access_token

    def send(self, token: str, data: dict[str, str]) -> None:
        priority = "HIGH" if data.get("priority") == "HIGH" else "NORMAL"
        body = {"message": {"token": token, "data": data, "android": {"priority": priority}}}
        try:
            access_token = self._access_token()
        except Exception:
            raise PushFailed("credential_error") from None
        try:
            response = self._http.post(
                self._path,
                json=body,
                headers={"Authorization": f"Bearer {access_token}"},
            )
        except httpx.TimeoutException:
            raise PushFailed("timeout") from None
        except httpx.HTTPError:
            raise PushFailed("transport_error") from None
        if response.status_code == httpx.codes.OK:
            return
        reason = f"http_{response.status_code}"
        if response.status_code in TRANSIENT:
            raise PushFailed(reason, _retry_after(response))
        raise PushRejected(reason)


class ServiceAccountAccessToken:
    """Access token de la cuenta de servicio, renovado por `google-auth` al vencer."""

    def __init__(self, service_account_file: Path) -> None:
        self._request = Request()
        credentials = service_account.Credentials
        self._credentials = credentials.from_service_account_file(  # type: ignore[no-untyped-call]
            str(service_account_file), scopes=[SCOPE]
        )

    def __call__(self) -> str:
        if not self._credentials.valid:
            self._credentials.refresh(self._request)
        token: str = self._credentials.token
        return token
