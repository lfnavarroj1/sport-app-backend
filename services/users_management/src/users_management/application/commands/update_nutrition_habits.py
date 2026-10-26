"""HU009: `PUT /v1/me/nutrition-habits` (ADR-035). Flujo común en `profile_section`.

Exige `consent.version` igual a `HEALTH_DATA_CONSENT_VERSION` (alergias y restricciones son
datos de salud); sin consentimiento vigente no se persiste nada. Registrar hábitos no crea
planes, consumos ni recomendaciones: solo guarda la sección y publica
`habitos.alimenticios_actualizados.v1` con su estado completo.
"""

from collections.abc import Callable
from uuid import UUID, uuid4

from users_management.application.commands.profile_section import UpdateProfileSection
from users_management.application.ports import Clock, ProfileUnitOfWork
from users_management.domain.profile.events import ProfileSection
from users_management.domain.profile.nutrition_habits import (
    NutritionHabitsInput,
    NutritionHabitsSection,
    validate_nutrition_habits,
)

# Nombre fijado por el paquete de HU009 (`operation` de `idempotency_key`).
OPERATION = "put_nutrition_habits"


class UpdateNutritionHabits(UpdateProfileSection[NutritionHabitsInput]):
    section = ProfileSection.NUTRITION_HABITS
    operation = OPERATION

    def __init__(
        self,
        *,
        unit_of_work: Callable[[], ProfileUnitOfWork],
        clock: Clock,
        current_consent_version: str,
        new_id: Callable[[], UUID] = uuid4,
    ) -> None:
        super().__init__(unit_of_work=unit_of_work, clock=clock, new_id=new_id)
        self._current_consent_version = current_consent_version

    def validate(self, section_input: NutritionHabitsInput) -> NutritionHabitsSection:
        return validate_nutrition_habits(section_input, self._current_consent_version)
