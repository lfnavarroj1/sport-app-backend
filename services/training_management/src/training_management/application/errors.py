"""Errores de la capa de aplicación, con códigos estables para el contrato de HU010."""


class IdempotencyKeyReused(Exception):
    """La misma clave se usó con una solicitud diferente."""

    code = "idempotency_key_reused"


class IdempotencyKeyConflict(Exception):
    """Otra solicitud concurrente confirmó primero la misma clave."""


class PermissionRequired(Exception):
    """El tipo de actor no tiene el permiso de la operación (ADR-025)."""

    code = "permission_required"


class CapabilityRequired(Exception):
    """El deportista no tiene la capacidad vigente de su plan (ADR-024)."""

    code = "capability_required"


class AthleteAuthorizationRequired(Exception):
    """CA2: no existe una autorización activa del deportista para este profesional."""

    code = "athlete_authorization_required"


class CapabilitiesUnavailable(Exception):
    """Afiliación no permitió evaluar las capacidades; nunca se conceden por suposición."""

    code = "upstream_unavailable"


class TrainingPlanNotFound(Exception):
    """No existe, no es visible para quien consulta o aún no está proyectado."""

    code = "training_plan_not_found"


class AuthorizationConflict(Exception):
    """Otra solicitud concurrente creó primero la misma autorización."""


class CapabilitiesTimeout(CapabilitiesUnavailable):
    """Afiliación no respondió a tiempo (HU032 responde `504`; HU010 lo trata como `503`)."""

    code = "upstream_timeout"


class ActiveSessionExists(Exception):
    """HU032: el deportista ya tiene una sesión `ACTIVE` (restricción única de la base)."""

    code = "active_session_exists"


class SessionIdReused(Exception):
    """HU032: el `session_id` ya existe con otro inicio o pertenece a otra persona."""

    code = "session_id_reused"


class TrainingSessionNotFound(Exception):
    """HU032: la sesión no existe o no pertenece a quien llama."""

    code = "training_session_not_found"


class TrainingSessionAlreadyFinished(Exception):
    """HU032: la sesión ya terminó con otro `ended_at`."""

    code = "training_session_already_finished"


class SessionIdConflict(Exception):
    """Otra solicitud concurrente insertó primero el mismo `session_id`."""
