"""`entrenamiento.finalizado.v1` (CMP-003; payload aprobado el 2026-10-12 para HU011).

Lo publicará HU032 al cerrar una sesión; HU011 lo consume para el historial. Clave
de orden y agregado: `session_id`. El payload no lleva métricas de salud, ubicación
ni mediciones (HU033-HU037 las definirán en sus propios contratos).
"""

from enum import StrEnum

EVENT_TYPE = "entrenamiento.finalizado.v1"
SCHEMA_VERSION = 1
AGGREGATE_TYPE = "training_session"


class SessionStatus(StrEnum):
    FINISHED = "FINISHED"
