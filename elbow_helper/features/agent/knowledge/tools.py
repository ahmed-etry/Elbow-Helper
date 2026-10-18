"""Permission-aware searches of community reference sections."""

import asyncio

from elbow_helper.infrastructure.ai import AgentToolDefinition
from ..access import has_access_requirements, require_lookup_access, require_evidence_access

from ..engine.capability_contract import CapabilityContract
from ..models import RegisteredAgentTool
from .store import VISIBILITIES


def knowledge_tools():
    return (RegisteredAgentTool(AgentToolDefinition(
        "search_approved_knowledge",
        "Open knowledge by exact topic-index titles, or search with query; titles take precedence.",
        {"type":"object", "properties":{
            "query":{"type":"string", "minLength":0, "maxLength":200},
            "titles":{"type":"array", "minItems":1, "maxItems":5, "uniqueItems":True,
                      "items":{"type":"string"}}},
         "required":[], "additionalProperties":False}), search_approved_knowledge,
        contract=CapabilityContract(()), returns="sections:[{title,body,visibility}]"),)


async def search_approved_knowledge(context, arguments):
    await require_evidence_access(context)
    if context.knowledge_store is None:
        return {"sections": []}
    catalog = await asyncio.to_thread(context.knowledge_store.load)
    levels = {level for level in VISIBILITIES - {"public"}
              if has_access_requirements(context.guild, context.member.id, {level})}
    visible = catalog.visible_sections(levels)
    if "titles" in arguments:
        sections = tuple(section for title in dict.fromkeys(arguments["titles"])
                         for section in visible if section.title == title)
    elif "query" in arguments:
        sections = catalog.search(visible, query=arguments["query"])
    else:
        sections = ()
    levels = {section.visibility for section in sections if section.visibility != "public"}
    if levels:
        require_lookup_access(context, levels)
    await require_evidence_access(context)
    context.state.required_access.update(levels)
    return {"sections":[{"title":section.title, "body":section.body[:4000],
                         "visibility":section.visibility} for section in sections]}
