"""Contrato de los eventos del perfil (HU005 y HU006, ADR-034; HU009, ADR-035).

Cada sección es un agregado propio con `aggregate_id = ordering_key = user_id` y versión
desde 1. El payload es el estado completo de la sección tal como lo devuelve el comando
(incluye `user_id`); así el proyector puede reconstruir la vista sin leer la escritura.
"""

from enum import StrEnum

SCHEMA_VERSION = 1


class ProfileSection(StrEnum):
    DEMOGRAPHIC = "demographic"
    PHYSIOLOGICAL = "physiological"
    SPORT_HABITS = "sport_habits"
    NUTRITION_HABITS = "nutrition_habits"


AGGREGATE_TYPE_BY_SECTION: dict[ProfileSection, str] = {
    ProfileSection.DEMOGRAPHIC: "demographic_profile",
    ProfileSection.PHYSIOLOGICAL: "physiological_profile",
    ProfileSection.SPORT_HABITS: "sport_habits",
    ProfileSection.NUTRITION_HABITS: "nutrition_habits",
}

EVENT_TYPE_BY_SECTION: dict[ProfileSection, str] = {
    ProfileSection.DEMOGRAPHIC: "perfil.demografico_actualizado.v1",
    ProfileSection.PHYSIOLOGICAL: "perfil.fisiologico_actualizado.v1",
    ProfileSection.SPORT_HABITS: "habitos.deportivos_actualizados.v1",
    ProfileSection.NUTRITION_HABITS: "habitos.alimenticios_actualizados.v1",
}

SECTION_BY_EVENT_TYPE: dict[str, ProfileSection] = {
    event_type: section for section, event_type in EVENT_TYPE_BY_SECTION.items()
}
