"""Construct read tools from their ordered definitions and contracts."""

from elbow_helper.infrastructure.ai import AgentToolDefinition
from .models import RegisteredAgentTool


def read_tools(definitions, contracts, usage_notes=None):
    notes = usage_notes or {}
    return tuple(RegisteredAgentTool(
        AgentToolDefinition(
            name=name, description=description,
            parameters={
                "type": "object", "properties": properties,
                "required": list(required), "additionalProperties": False,
            },
            usage_note=notes.get(name, ""),
        ), handler, contract=contracts[name],
    ) for name, description, properties, required, handler in definitions)
