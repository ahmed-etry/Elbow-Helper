"""Leadership record categories and incident types."""

from __future__ import annotations

from dataclasses import dataclass

from discord import app_commands

CATEGORY_CWL = "cwl"
CATEGORY_WAR = "war"
CATEGORY_MEMBERSHIP = "membership"
CATEGORY_COMMUNICATION = "communication"


@dataclass(frozen=True)
class RecordCategory:
    key: str
    label: str


@dataclass(frozen=True)
class IncidentType:
    key: str
    label: str
    category_key: str


RECORD_CATEGORIES: tuple[RecordCategory, ...] = (
    RecordCategory(CATEGORY_CWL, "CWL"),
    RecordCategory(CATEGORY_WAR, "War"),
    RecordCategory(CATEGORY_MEMBERSHIP, "Membership"),
    RecordCategory(CATEGORY_COMMUNICATION, "Communication"),
)

INCIDENT_TYPES: tuple[IncidentType, ...] = (
    IncidentType("cwl_missed_attacks", "Missed Attack", CATEGORY_CWL),
    IncidentType("cwl_missed_transfer", "Missed Transfer", CATEGORY_CWL),
    IncidentType("cwl_unreliability", "Unreliability", CATEGORY_CWL),
    IncidentType("cwl_other", "Other", CATEGORY_CWL),
    IncidentType("war_missed_attacks", "Missed Attack", CATEGORY_WAR),
    IncidentType("war_first_claim", "Attacked Another Player's First Claim", CATEGORY_WAR),
    IncidentType("war_rules", "Broke War Rules", CATEGORY_WAR),
    IncidentType("war_other", "Other", CATEGORY_WAR),
    IncidentType("membership_disrespect", "Disrespect", CATEGORY_MEMBERSHIP),
    IncidentType("membership_disruption", "Disruptive Behavior", CATEGORY_MEMBERSHIP),
    IncidentType("membership_clan_hopping", "Clan Hopping", CATEGORY_MEMBERSHIP),
    IncidentType("membership_other", "Other", CATEGORY_MEMBERSHIP),
    IncidentType("communication_no_response", "Failure to Respond", CATEGORY_COMMUNICATION),
    IncidentType("communication_commitment", "Repeatedly Missed Commitments", CATEGORY_COMMUNICATION),
    IncidentType("communication_other", "Other", CATEGORY_COMMUNICATION),
)

CATEGORY_BY_KEY = {category.key: category for category in RECORD_CATEGORIES}
INCIDENT_TYPE_BY_KEY = {incident.key: incident for incident in INCIDENT_TYPES}

CATEGORY_CHOICES = [
    app_commands.Choice(name=category.label, value=category.key)
    for category in RECORD_CATEGORIES
]


def category_label(category_key: str) -> str:
    category = CATEGORY_BY_KEY.get(str(category_key or ""))
    return category.label if category else str(category_key or "-")


def incident_type_label(incident_type_key: str) -> str:
    incident = INCIDENT_TYPE_BY_KEY.get(str(incident_type_key or ""))
    return incident.label if incident else str(incident_type_key or "-")


def incident_types_for_category(category_key: str) -> tuple[IncidentType, ...]:
    return tuple(item for item in INCIDENT_TYPES if item.category_key == category_key)


def resolve_incident_type(category_key: str, value: str) -> str | None:
    normalized = str(value or "").strip().casefold()
    for incident in incident_types_for_category(category_key):
        if normalized in {incident.key.casefold(), incident.label.casefold()}:
            return incident.key
    return None
