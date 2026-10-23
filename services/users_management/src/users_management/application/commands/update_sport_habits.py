"""HU006: `PUT /v1/me/sport-habits` (ADR-034). Flujo común en `profile_section`.

Registrar hábitos no crea planes ni recomendaciones: solo guarda la sección y publica
`habitos.deportivos_actualizados.v1` con su estado completo.
"""

from users_management.application.commands.profile_section import UpdateProfileSection
from users_management.domain.profile.events import ProfileSection
from users_management.domain.profile.sport_habits import (
    SportHabitsInput,
    SportHabitsSection,
    validate_sport_habits,
)

OPERATION = "update_sport_habits"


class UpdateSportHabits(UpdateProfileSection[SportHabitsInput]):
    section = ProfileSection.SPORT_HABITS
    operation = OPERATION

    def validate(self, section_input: SportHabitsInput) -> SportHabitsSection:
        return validate_sport_habits(section_input)
