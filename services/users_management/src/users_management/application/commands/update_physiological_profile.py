"""HU005: `PUT /v1/me/physiological-profile` (ADR-034). Flujo en `profile_section`.

Exige `consent.version` igual a `HEALTH_DATA_CONSENT_VERSION`; sin consentimiento
vigente no se persiste nada (CA3).
"""

from collections.abc import Callable
from uuid import UUID, uuid4

from users_management.application.commands.profile_section import UpdateProfileSection
from users_management.application.ports import Clock, ProfileUnitOfWork
from users_management.domain.profile.events import ProfileSection
from users_management.domain.profile.physiological import (
    PhysiologicalInput,
    PhysiologicalSection,
    validate_physiological,
)

OPERATION = "update_physiological_profile"


class UpdatePhysiologicalProfile(UpdateProfileSection[PhysiologicalInput]):
    section = ProfileSection.PHYSIOLOGICAL
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

    def validate(self, section_input: PhysiologicalInput) -> PhysiologicalSection:
        return validate_physiological(
            section_input, self._clock.now(), self._current_consent_version
        )
