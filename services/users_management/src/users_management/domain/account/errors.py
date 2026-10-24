"""Errores de negocio de la cuenta, con códigos estables para el contrato."""


class AccountError(Exception):
    code = "account_error"


class InvalidRegistrationData(AccountError):
    code = "validation_error"

    def __init__(self, field: str, issue: str) -> None:
        super().__init__(f"{field}: {issue}")
        self.field = field
        self.issue = issue


class PoliciesVersionMismatch(AccountError):
    code = "policies_version_mismatch"


class EmailAlreadyRegistered(AccountError):
    code = "email_already_registered"


class AccountVersionConflict(AccountError):
    """HU002: `expected_version` no coincide con la versión vigente de la cuenta."""

    code = "version_conflict"

    def __init__(self, current_version: int) -> None:
        super().__init__(f"current_version={current_version}")
        self.current_version = current_version
