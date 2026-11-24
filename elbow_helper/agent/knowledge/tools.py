"""Permission-aware searches of community reference sections."""

import asyncio
import hashlib
from dataclasses import replace

from elbow_helper.infrastructure.ai import AgentToolDefinition
from elbow_helper.agent.safety.access import has_reader_access_requirements, require_evidence_access, require_lookup_access
from elbow_helper.agent.safety.disclosure import request_scope, exclude_source

from ..engine.capability_contract import CapabilityContract
from elbow_helper.agent.engine.models import RegisteredAgentTool
from .store import VISIBILITIES


MAX_SEARCHES = 5
HITS_PER_PAGE = 30


def knowledge_tools():
    return (RegisteredAgentTool(AgentToolDefinition(
        "search_approved_knowledge",
        "Searches note text with several wordings and returns matching lines with their section.",
        {"type":"object", "properties":{
            "query":{"type":"string", "minLength":0, "maxLength":200},
            "searches":{"type":"array", "minItems":1, "maxItems":MAX_SEARCHES,
                        "description":"Wordings to look for in note text. With titles, only those sections or files are searched.",
                        "items":{"type":"string", "minLength":1, "maxLength":200}},
            "titles":{"type":"array", "minItems":1, "maxItems":5, "uniqueItems":True,
                      "description":"Exact indexed headings or filenames. Query searches within selected files.",
                      "items":{"type":"string"}},
            "section_id":{"type":"string", "minLength":SECTION_ID_LENGTH, "maxLength":64},
            "offset":{"type":"integer", "minimum":0},
            "limit":{"type":"integer", "minimum":1, "maximum":8},
            "section_offset":{"type":"integer", "minimum":0}},
         "required":[], "additionalProperties":False},
        usage_note=("Up to 5 searches; word forms match (post, posted). A hit is a line with its section title and "
                    "file, most searched words first; offset pages on. titles open full sections; query ranks "
                    "whole sections.")), search_approved_knowledge,
        contract=CapabilityContract(())),)


async def search_approved_knowledge(context, arguments):
    await require_evidence_access(context)
    if context.knowledge_store is None:
        return _unavailable()
    try:
        catalog = await asyncio.to_thread(context.knowledge_store.load)
    except (OSError, ValueError):
        return _unavailable()
    if catalog.store_status == "unavailable":
        return _unavailable()
    # Exact headings retain the larger page. Broad lexical searches start
    # with a short page, with an explicit cursor for any remaining matches.
    offset = arguments.get("offset", 0)
    limit = arguments.get("limit", 3 if arguments.get("query", "").strip() else 8)
    section_offset = arguments.get("section_offset", 0)
    if (type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 8
            or type(section_offset) is not int or section_offset < 0
            or section_offset and not (arguments.get("titles") or arguments.get("section_id"))):
        return {"error": "Use a valid knowledge page; section continuation needs a title or section ID."}
    active_scope = await request_scope(context)
    levels = active_scope.levels & (VISIBILITIES - {"public"})
    visible = catalog.visible_sections(levels)
    excluded = tuple(section for section in catalog.active_sections
                     if section.visibility != "public" and section.visibility not in levels)
    # Match only metadata outside the scope. No restricted body or title is
    # returned to the model, retained as evidence, or paged in a shared answer.
    metadata = tuple(replace(section, body="") for section in excluded
                     if has_reader_access_requirements(context.guild, context.member.id, {section.visibility}))
    searches = arguments.get("searches")
    if searches is not None and not (isinstance(searches, list) and 1 <= len(searches) <= MAX_SEARCHES
                                     and all(isinstance(search, str) for search in searches)):
        return {"error": f"Use 1 to {MAX_SEARCHES} searches, each a string."}
    if searches:
        # A restricted section counts as relevant by its title only; its lines are never searched.
        # titles narrow what is returned: the model cannot name a section it was never shown.
        relevant = tuple(dict.fromkeys(section for search in searches
                                       for section in catalog.search(metadata, query=search, limit=None)))
    elif arguments.get("query", "").strip():
        relevant = catalog.search(metadata, query=arguments["query"], limit=None)
    elif arguments.get("titles"):
        relevant, _ = catalog.select(metadata, titles=arguments["titles"])
    elif "section_id" in arguments:
        relevant = tuple(section for section in excluded if has_section_id(section, arguments["section_id"])
                         and has_reader_access_requirements(context.guild, context.member.id, {section.visibility}))
    else:
        relevant = ()
    relevant_titles = {(section.file_name, section.title) for section in relevant}
    for section in excluded:
        exclude_source(context, "knowledge:" + _section_id(section), "role_scope",
                       relevant=(section.file_name, section.title) in relevant_titles)
    if searches:
        pool, _ = catalog.select(visible, titles=arguments.get("titles", ()))
        hits = await asyncio.to_thread(catalog.matching_lines, pool, searches)
        shown = hits[offset:offset + HITS_PER_PAGE]
        levels = {section.visibility for section, _ in shown if section.visibility != "public"}
        await require_evidence_access(context)
        require_lookup_access(context, levels)
        context.state.required_access.update(levels)
        context.state.knowledge_sources.update(_section_id(section) for section, _ in shown)
        more = len(hits) - offset - len(shown)
        return {"hits": [{"line": line, "title": section.title, "file": section.file_name,
                          "section_id": _section_id(section), "visibility": section.visibility}
                         for section, line in shown],
                "total_hits": len(hits), "more_hits": more, "next_offset": offset + len(shown) if more else None,
                "store_status": catalog.store_status, "scope": "requester" if active_scope.requester else "channel",
                "available_privately": bool(relevant),
                "match_status": "matches" if hits else "no_visible_match",
                "observed_at": catalog.observed_at, "offset": offset,
                "search_is_exhaustive": False, "search_complete": False,
                "coverage": {"scope": "visible_sections", "method": "matching_lines",
                             "matching_lines": len(hits), "returned_lines": len(shown),
                             "store_complete": catalog.store_status == "available"},
                "limitations": {"lexical_search_only": True,
                                "store_incomplete": catalog.store_status != "available"}}
    if "section_id" in arguments:
        sections = tuple(section for section in visible if has_section_id(section, arguments["section_id"]))
        method = "section_id"
    else:
        sections, method = catalog.select(visible, titles=arguments.get("titles", ()),
                                          query=arguments.get("query", ""))
    if "limit" not in arguments and method != "lexical_ranked":
        limit = 8
    total = len(sections)
    sections = sections[offset:offset + limit]
    levels = {section.visibility for section in sections if section.visibility != "public"}
    await require_evidence_access(context)
    require_lookup_access(context, levels)
    context.state.required_access.update(levels)
    returned = []
    for section in sections:
        body = section.body[section_offset:section_offset + 4000]
        end = section_offset + len(body)
        page = {"title": section.title, "section_id": _section_id(section), "body": body,
                "part": section.part, "parts": section.parts,
                **({"applicability": section.applicability} if section.applicability else {}),
                "visibility": section.visibility, "section_offset": section_offset,
                "total_characters": len(section.body), "truncated": end < len(section.body),
                "next_section_offset": end if end < len(section.body) else None}
        context.state.knowledge_sources.add(page["section_id"])
        returned.append(page)
    next_offset = offset + len(sections) if offset + len(sections) < total else None
    clipped = any(section.get("truncated", False) for section in returned)
    exhaustive = catalog.store_status == "available" and method != "lexical_ranked"
    return {"sections": returned, "store_status": catalog.store_status, "scope": "requester" if active_scope.requester else "channel",
            "available_privately": bool(relevant),
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
    active = (await request_scope(context)).levels & (VISIBILITIES - {"public"})
    return set(active if levels is None else active & levels)


SECTION_ID_LENGTH = 12


def _section_digest(section):
    return hashlib.sha256((section.file_name + "\0" + section.title + "\0" + section.body
                           + (f"\0{section.part}" if section.parts > 1 else "")).encode()).hexdigest()


def _section_id(section):
    return _section_digest(section)[:SECTION_ID_LENGTH]


def has_section_id(section, identifier):
    """An ID is the start of the section's digest, so full digests shown earlier still open it."""
    return len(identifier) >= SECTION_ID_LENGTH and _section_digest(section).startswith(identifier)


def _unavailable():
    return {"sections": [], "store_status": "unavailable", "match_status": "store_unavailable",
            "search_complete": False, "search_is_exhaustive": False,
            "limitations": {"store_unavailable": True},
            "flags": {"status": "failed", "truncated": False, "limits": ["store_unavailable"]}}
