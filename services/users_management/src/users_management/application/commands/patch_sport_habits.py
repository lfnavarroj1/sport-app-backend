"""HU008: `PATCH /v1/me/sport-habits` (ADR-035). Flujo común en `profile_section`.

Edita el agregado `sport_habits` de HU006: cada sección enviada reemplaza a la vigente, las
demás se conservan y se revalida el agregado completo. Exige la sección registrada
(`404 sport_habits_not_found`) y `expected_version`. Publica el mismo
`habitos.deportivos_actualizados.v1` con la versión nueva y el estado completo; es la señal
para Entrenamientos, que no modifica planes vigentes (CA4).
"""

from datetime import datetime
from typing import Any

from users_management.application.commands.profile_section import UpdateProfileSection
from users_management.application.ports import StoredProfileSection
from users_management.domain.profile.events import ProfileSection
from users_management.domain.profile.section import ValidSection
from users_management.domain.profile.sport_habits import (
    NOT_FOUND_CODE,
    SportHabitsPatch,
    SportHabitsPatchInput,
    validate_sport_habits_patch,
)

OPERATION = "patch_sport_habits"


class PatchSportHabits(UpdateProfileSection[SportHabitsPatchInput]):
    section = ProfileSection.SPORT_HABITS
    operation = OPERATION
    not_found_code = NOT_FOUND_CODE
    updated_outcome = "updated"

    def validate(self, section_input: SportHabitsPatchInput) -> SportHabitsPatch:
        return validate_sport_habits_patch(section_input)

    def section_fields(
        self, valid: ValidSection, current: StoredProfileSection | None, now: datetime
    ) -> dict[str, Any]:
        if not isinstance(valid, SportHabitsPatch) or current is None:
            raise TypeError("PatchSportHabits fusiona un SportHabitsPatch con la sección vigente")
        return valid.merge(current.document, now)
