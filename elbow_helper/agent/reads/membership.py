"""Bounded membership observations from the family record's clan stays.

Stays are the record's roster observations, with the old clan health file's
hourly stays copied into it for the time before the record began. An arrival is
when an account was first seen in a clan, not when it joined.
"""

from elbow_helper.configuration.clans import CLANS
from elbow_helper.infrastructure.ai import AgentToolDefinition

from elbow_helper.agent.engine.capability_contract import CapabilityContract
from elbow_helper.agent.models import RegisteredAgentTool
from elbow_helper.agent.reads.people import read_membership_history

def membership_tools():
    return (RegisteredAgentTool(AgentToolDefinition(
        "read_membership_history", "Read bounded newcomer and returner observations from family clan stays.",
        {"type": "object", "properties": {
            "after": {"type": "string", "maxLength": 40},
            "before": {"type": "string", "maxLength": 40},
            "history_after": {"type": "string", "maxLength": 40},
            "clan_codes": {"type": "array", "maxItems": 20,
                           "items": {"type": "string", "enum": sorted(CLANS)}},
            "offset": {"type": "integer", "minimum": 0, "default": 0},
            "limit": {"type": "integer", "minimum": 1, "maximum": 25, "default": 25},
        }, "required": ["after"], "additionalProperties": False},
        usage_note="Max 90 days of family clan stays. First observed is not first-ever membership; returns require earlier observations inside bounds.",
    ), read_membership_history, contract=CapabilityContract()),)
