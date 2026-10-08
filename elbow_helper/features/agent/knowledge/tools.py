"""Permission-aware searches of community reference sections."""

import asyncio

from elbow_helper.infrastructure.ai import AgentToolDefinition
from ..access import has_access_requirements, require_lookup_access, require_evidence_access

from ..engine.capability_contract import CapabilityContract
from ..models import RegisteredAgentTool


def knowledge_tools():
    return (RegisteredAgentTool(AgentToolDefinition(
        "search_approved_knowledge", "Search approved community knowledge visible to the asker.",
        {"type":"object", "properties":{"query":{"type":"string", "maxLength":200}},
         "required":["query"], "additionalProperties":False}), search_approved_knowledge,
        contract=CapabilityContract(()), returns="sections:[{title,body,visibility}]"),)


async def search_approved_knowledge(context, arguments):
    await require_evidence_access(context)
    if context.knowledge_store is None:
        return {"sections": []}
    catalog = await asyncio.to_thread(context.knowledge_store.load)
    visible = tuple(section for section in catalog.active_sections
        if section.visibility == "public" or has_access_requirements(
            context.guild, context.member.id, {section.visibility}))
    sections = catalog.search(visible, query=arguments["query"])
    levels = {section.visibility for section in sections if section.visibility != "public"}
    if levels:
        require_lookup_access(context, levels)
    await require_evidence_access(context)
    context.state.required_access.update(levels)
    return {"sections":[{"title":section.title, "body":section.body[:4000],
                         "visibility":section.visibility} for section in sections]}
