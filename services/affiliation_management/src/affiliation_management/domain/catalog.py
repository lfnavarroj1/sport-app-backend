"""Catálogo de planes y capacidades aprobado en ADR-024.

Los datos completos (nombres, textos, precios) los precargan las migraciones
`0002_free_plan_catalog` y `0003_plan_price_offers` (ADR-036). Este módulo conserva solo
lo que el código necesita para decidir: códigos estables, plan predeterminado del
deportista y el conjunto obligatorio que la sonda de disponibilidad exige.
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

# Periodicidades de las ofertas que debe tener cada plan en el catálogo esperado (ADR-036).
OFFER_PERIODICITIES_BY_PLAN = {
    FREE_PLAN: ("NONE",),
    INTERMEDIATE_PLAN: ("MONTHLY", "QUARTERLY", "ANNUAL"),
    PREMIUM_PLAN: ("MONTHLY", "QUARTERLY", "ANNUAL"),
}
