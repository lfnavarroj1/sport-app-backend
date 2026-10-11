"""Catálogo de planes y capacidades aprobado en ADR-024.

Los datos completos (nombres, textos, precios) los precarga la migración
`0002_free_plan_catalog`. Este módulo conserva solo lo que el código necesita
para decidir: códigos estables, plan predeterminado del deportista y el conjunto
obligatorio que la sonda de disponibilidad exige.
"""

FREE_PLAN = "FREE"
INTERMEDIATE_PLAN = "INTERMEDIATE"
PREMIUM_PLAN = "PREMIUM"
PLAN_CODES = (FREE_PLAN, INTERMEDIATE_PLAN, PREMIUM_PLAN)

FREE_CAPABILITIES = (
    "training.plan.manage",
    "training.routes.suggest",
    "events.suggest",
    "training.plans.suggest",
    "services.support.suggest",
    "nutrition.routines.suggest",
    "integrations.strava.connect",
    "training.session.record",
    "training.dashboard.view",
    "training.metrics.ftp_vo2max",
    "calendar.sport.view",
)
INTERMEDIATE_CAPABILITIES = (
    *FREE_CAPABILITIES,
    "training.realtime.monitor",
    "devices.measurement.connect",
    "nutrition.session.reminders",
)
PREMIUM_CAPABILITIES = (
    *INTERMEDIATE_CAPABILITIES,
    "services.sports_medicine.access",
    "services.virtual_sessions.schedule",
)
CAPABILITIES_BY_PLAN = {
    FREE_PLAN: FREE_CAPABILITIES,
    INTERMEDIATE_PLAN: INTERMEDIATE_CAPABILITIES,
    PREMIUM_PLAN: PREMIUM_CAPABILITIES,
}
REQUIRED_CAPABILITIES = PREMIUM_CAPABILITIES
