"""Configuración de Strava: comprobación de `/health/ready` y construcción del contexto.

Sin `STRAVA_CLIENT_ID`, `STRAVA_CLIENT_SECRET`, `STRAVA_REDIRECT_URI`,
`STRAVA_CONSENT_VERSION` o una `STRAVA_TOKEN_KEY` de 32 bytes el servicio arranca, pero no
está listo y las operaciones que dependen de Strava responden `503` (R). Nunca se registran
los valores, solo los nombres de las variables que faltan.
"""

import logging

import httpx

from third_party_management.application.ports import StravaContext
from third_party_management.infrastructure.oauth_state import HmacStateCodec
from third_party_management.infrastructure.strava.circuit import CircuitBreaker
from third_party_management.infrastructure.strava.client import (
    StravaCredentials,
    StravaHttpGateway,
)
from third_party_management.infrastructure.token_cipher import (
    AesGcmTokenSealer,
    InvalidEncryptionKey,
    decode_key,
    derive_state_key,
)
from third_party_management.settings import ThirdPartyManagementSettings

READINESS_CHECK = "strava_configuration"

_logger = logging.getLogger("sportapp.strava")


def token_key(settings: ThirdPartyManagementSettings) -> bytes | None:
    if settings.strava_token_key is None:
        return None
    try:
        return decode_key(settings.strava_token_key.get_secret_value())
    except InvalidEncryptionKey:
        return None


def missing_strava_settings(settings: ThirdPartyManagementSettings) -> list[str]:
    """Nombres de las variables ausentes o inválidas (nunca sus valores)."""
    secret = settings.strava_client_secret
    present = {
        "STRAVA_CLIENT_ID": bool(settings.strava_client_id),
        "STRAVA_CLIENT_SECRET": secret is not None and bool(secret.get_secret_value()),
        "STRAVA_REDIRECT_URI": bool(settings.strava_redirect_uri),
        "STRAVA_CONSENT_VERSION": bool(settings.strava_consent_version),
        "STRAVA_TOKEN_KEY": token_key(settings) is not None,
    }
    return [name for name, ok in present.items() if not ok]


class StravaConfigurationCheck:
    def __init__(self, settings: ThirdPartyManagementSettings) -> None:
        self._missing = missing_strava_settings(settings)

    @property
    def name(self) -> str:
        return READINESS_CHECK

    def __call__(self) -> bool:
        if self._missing:
            _logger.warning(
                "strava.configuration_incomplete", extra={"fields": {"missing": self._missing}}
            )
            return False
        return True


def build_strava_context(
    settings: ThirdPartyManagementSettings,
    http: httpx.Client,
    breaker: CircuitBreaker | None = None,
) -> StravaContext | None:
    key = token_key(settings)
    if missing_strava_settings(settings) or key is None:
        return None
    # `missing_strava_settings` vacío garantiza los valores; se repite para el tipado.
    client_id, secret = settings.strava_client_id, settings.strava_client_secret
    redirect_uri, consent = settings.strava_redirect_uri, settings.strava_consent_version
    if client_id is None or secret is None or redirect_uri is None or consent is None:
        return None
    gateway = StravaHttpGateway(
        http,
        StravaCredentials(
            client_id=client_id,
            client_secret=secret.get_secret_value(),
            redirect_uri=redirect_uri,
            authorize_url=settings.strava_authorize_url,
        ),
        breaker=breaker,
    )
    return StravaContext(
        gateway=gateway,
        sealer=AesGcmTokenSealer(key),
        states=HmacStateCodec(derive_state_key(key)),
        consent_version=consent,
    )
