"""Stored data query capability."""

from elbow_helper.infrastructure.ai import AgentToolDefinition

from ...models import RegisteredAgentTool
from ...engine.capability_contract import CapabilityContract
from .query import DEFAULT_QUERY_ROWS, query_bot_data


def dataset_tools():
    return (RegisteredAgentTool(
        AgentToolDefinition(
            "query_bot_data",
            "Query SQL: :name, params:{name:value}.",
            {
                "type": "object",
                "properties": {
                    "sql": {"type": "string", "minLength": 1, "maxLength": 4000},
                    "params": {"type": "object", "additionalProperties": True},
                    "max_rows": {
                        "type": "integer", "minimum": 1, "maximum": 1000, "default": DEFAULT_QUERY_ROWS,
                    },
                },
                "required": ["sql"], "additionalProperties": False,
            },
            usage_note=(
                "Prefer one SQL query for the whole question. Let SQL count, group, join, "
                "filter and sort."
            ),
        ),
        query_bot_data,
        contract=CapabilityContract(), returns="rows[] with the selected columns",
    ),)
