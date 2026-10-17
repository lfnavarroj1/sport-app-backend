"""HU005: `PUT /v1/me/demographic-profile` (ADR-034). Flujo en `profile_section`."""

from datetime import UTC

from users_management.application.commands.profile_section import UpdateProfileSection
from users_management.domain.profile.demographic import (
    DemographicInput,
    DemographicSection,
    validate_demographic,
)
from users_management.domain.profile.events import ProfileSection

OPERATION = "update_demographic_profile"


class UpdateDemographicProfile(UpdateProfileSection[DemographicInput]):
    section = ProfileSection.DEMOGRAPHIC
    operation = OPERATION

    def validate(self, section_input: DemographicInput) -> DemographicSection:
        # La edad se calcula sobre la fecha UTC del comando.
        return validate_demographic(section_input, self._clock.now().astimezone(UTC).date())
