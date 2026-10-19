"""Capacidades del deportista desde el contrato interno de Afiliación (OpenAPI 0.2.0).

`GET /v1/subscriptions/by-user/{user_id}/capabilities` responde `ACTIVE` con las
capacidades vigentes o `NONE` sin ellas. Cualquier otra respuesta, un error de red
o un timeout es indisponibilidad: nunca se concede una capacidad por suposición. El
timeout se distingue (`CapabilitiesTimeout`, subclase) para registrarlo; HU020 responde
`503` en ambos casos (ADR-038). Se llama fuera de cualquier transacción local.
"""

from uuid import UUID

import httpx

from complementary_services.application.errors import CapabilitiesTimeout, CapabilitiesUnavailable
from sportapp_platform.correlation import CORRELATION_HEADER, current_correlation_id


class AffiliationCapabilityChecker:
    def __init__(self, http: httpx.Client) -> None:
        self._http = http

    def has_capability(self, user_id: UUID, capability: str) -> bool:
        correlation_id = current_correlation_id()
        headers = {CORRELATION_HEADER: correlation_id} if correlation_id else {}
        try:
            response = self._http.get(
                f"/v1/subscriptions/by-user/{user_id}/capabilities", headers=headers
            )
        except httpx.TimeoutException:
            raise CapabilitiesTimeout() from None
        except httpx.HTTPError:
            raise CapabilitiesUnavailable() from None
        if response.status_code != httpx.codes.OK:
            raise CapabilitiesUnavailable()
        try:
            document = response.json()
        except ValueError:
            raise CapabilitiesUnavailable() from None
        if not isinstance(document, dict):
            raise CapabilitiesUnavailable()
        capabilities = document.get("capabilities")
        return (
            document.get("subscription_status") == "ACTIVE"
            and isinstance(capabilities, list)
            and capability in capabilities
        )
