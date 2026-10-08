"""Stored data query capability."""

from elbow_helper.infrastructure.ai import AgentToolDefinition

from ..models import RegisteredAgentTool
from ..engine.capability_contract import CapabilityContract
from .query import query_bot_data


def dataset_tools():
    return (RegisteredAgentTool(
        AgentToolDefinition(
            "query_bot_data",
            "Query stored bot data with read-only SQLite at the asker's access level.",
            {
                "type": "object",
                "properties": {
                    "sql": {"type": "string", "minLength": 1, "maxLength": 4000},
                    "params": {"type": "object", "additionalProperties": True},
                    "max_rows": {
                        "type": "integer", "minimum": 1, "maximum": 1000, "default": 200,
                    },
                },
                "required": ["sql"], "additionalProperties": False,
            },
        ),
        query_bot_data,
        contract=CapabilityContract(), returns="rows[] with the selected columns",
    ),)
