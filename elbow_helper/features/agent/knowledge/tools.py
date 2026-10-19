"""Permission-aware searches of community reference sections."""

import asyncio
import hashlib

from elbow_helper.infrastructure.ai import AgentToolDefinition
from ..access import has_access_requirements, require_lookup_access, require_evidence_access
from ..disclosure import can_show

from ..engine.capability_contract import CapabilityContract
from ..models import RegisteredAgentTool
from .store import VISIBILITIES


def knowledge_tools():
    return (RegisteredAgentTool(AgentToolDefinition(
        "search_approved_knowledge",
        "Open matching titles first; search all requester-accessible sections, with private bodies withheld.",
        {"type":"object", "properties":{
            "scope":{"type":"string", "enum":["channel", "requester"]},
            "query":{"type":"string", "minLength":0, "maxLength":200},
            "titles":{"type":"array", "minItems":1, "maxItems":5, "uniqueItems":True,
                      "items":{"type":"string"}},
            "section_id":{"type":"string", "minLength":64, "maxLength":64},
            "offset":{"type":"integer", "minimum":0},
            "limit":{"type":"integer", "minimum":1, "maximum":8},
            "section_offset":{"type":"integer", "minimum":0}},
         "required":[], "additionalProperties":False}), search_approved_knowledge,
        contract=CapabilityContract(()), returns="sections:[{title,body,visibility,available_privately}]"),)


async def search_approved_knowledge(context, arguments):
    await require_evidence_access(context)
    scope = arguments.get("scope", "requester")
    if scope not in ("channel", "requester"):
        return {"error": "Use channel or requester knowledge scope."}
    if context.knowledge_store is None:
        return _unavailable()
    try:
        catalog = await asyncio.to_thread(context.knowledge_store.load)
    except (OSError, ValueError):
        return _unavailable()
    if catalog.store_status == "unavailable":
        return _unavailable()
    offset, limit = arguments.get("offset", 0), arguments.get("limit", 8)
    section_offset = arguments.get("section_offset", 0)
    if (type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 8
            or type(section_offset) is not int or section_offset < 0
            or section_offset and not (arguments.get("titles") or arguments.get("section_id"))):
        return {"error": "Use a valid knowledge page; section continuation needs a title or section ID."}
    levels = {level for level in VISIBILITIES - {"public"}
              if has_access_requirements(context.guild, context.member.id, {level})}
    channel_levels = await channel_knowledge_access(context, levels)
    visible = catalog.visible_sections(levels)
    if "section_id" in arguments:
        sections = tuple(section for section in visible if _section_id(section) == arguments["section_id"])
        method = "section_id"
    elif "titles" in arguments:
        sections = tuple(section for title in dict.fromkeys(arguments["titles"])
                         for section in visible if section.title == title)
        method = "exact_titles"
    elif "query" in arguments:
        sections = catalog.search(visible, query=arguments["query"], limit=None)
        method = "lexical_ranked" if arguments["query"].strip() else "browse"
    else:
        sections = visible
        method = "browse"
    total = len(sections)
    sections = sections[offset:offset + limit]
    levels = {section.visibility for section in sections if section.visibility != "public"}
    if levels:
        require_lookup_access(context, levels)
    await require_evidence_access(context)
    context.state.required_access.update(levels & channel_levels)
    context.state.knowledge_title_access.update(levels - channel_levels)
    channel_titles = {section.title for section in catalog.visible_sections(channel_levels)}
    returned = []
    for section in sections:
        body = section.body[section_offset:section_offset + 4000]
        end = section_offset + len(body)
        page = {"title": section.title, "section_id": _section_id(section), "body": body,
                "visibility": section.visibility, "section_offset": section_offset,
                "total_characters": len(section.body), "truncated": end < len(section.body),
                "next_section_offset": end if end < len(section.body) else None}
        if section.visibility != "public" and section.visibility not in channel_levels:
            # Keep the body outside all model-facing results until the answer pass
            # explicitly needs it. Legacy scope arguments cannot bypass this gate.
            context.state.private_knowledge[page["section_id"]] = {
                **page, "body": section.body, "section_offset": 0,
                "truncated": False, "next_section_offset": None,
                "title_visible_in_channel": section.title in channel_titles,
            }
            returned.append({"title": section.title, "available_privately": True})
        else:
            returned.append(page)
    next_offset = offset + len(sections) if offset + len(sections) < total else None
    clipped = any(section.get("truncated", False) for section in returned)
    exhaustive = catalog.store_status == "available" and method != "lexical_ranked"
    return {"sections": returned, "store_status": catalog.store_status, "scope": "requester",
            "match_status": "matches" if total else "no_visible_match",
            "observed_at": catalog.observed_at, "offset": offset, "next_offset": next_offset,
            "search_is_exhaustive": exhaustive,
            "search_complete": exhaustive and next_offset is None,
            "coverage": {"scope": "visible_sections", "method": method,
                         "matching_sections": total, "returned_sections": len(returned),
                         "store_complete": catalog.store_status == "available"},
            "truncated": clipped,
            "limitations": {"lexical_search_only": method == "lexical_ranked",
                            "store_incomplete": catalog.store_status != "available",
                            "section_continuation_available": clipped}}


async def channel_knowledge_access(context, levels=None):
    if levels is None:
        levels = {level for level in VISIBILITIES - {"public"}
                  if has_access_requirements(context.guild, context.member.id, {level})}
    if not levels:
        return set()
    destination = getattr(context, "delivery_channel", None) or context.source_message.channel
    cache = getattr(context, "disclosure_thread_members", None)
    return {level for level in sorted(levels) if await can_show(
        destination, {}, {level}, context.guild,
        thread_members=cache if cache is not None else {}, log_decision=False,
    )}


def _section_id(section):
    return hashlib.sha256((section.file_name + "\0" + section.title + "\0" + section.body).encode()).hexdigest()


def _unavailable():
    return {"sections": [], "store_status": "unavailable", "match_status": "store_unavailable",
            "search_complete": False, "search_is_exhaustive": False,
            "limitations": {"store_unavailable": True},
            "flags": {"status": "failed", "truncated": False, "limits": ["store_unavailable"]}}
