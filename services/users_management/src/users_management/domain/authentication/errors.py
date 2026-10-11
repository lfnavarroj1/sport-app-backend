"""Errores de inicio de sesión (HU003), con códigos estables para el contrato."""

from users_management.domain.account.errors import AccountError


class InvalidCredentials(AccountError):
    """Mismo error para correo inexistente, contraseña incorrecta o cuenta deshabilitada:
    la respuesta no permite descubrir si un correo existe."""

    code = "invalid_credentials"


class TooManyLoginAttempts(AccountError):
    code = "too_many_attempts"

    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__(f"retry_after={retry_after_seconds}")
        self.retry_after_seconds = retry_after_seconds


class InvalidRefreshToken(AccountError):
    code = "invalid_refresh_token"


class RefreshTokenReused(InvalidRefreshToken):
    """Se presentó un refresh token ya rotado: posible robo; se revoca toda la familia."""
