"""Render lookup descriptions and input rules for the model.

Argument checks keep the full schema.
"""
from __future__ import annotations

from dataclasses import replace

from elbow_helper.agent.actions.contracts import ActionClass
from elbow_helper.agent.plan.input_rules import RULES, rule_sentences
from elbow_helper.agent.reports.tools import READ_NAME, original_tool

from .names import codes_line
from .wording import FINISH_REPLY_WITH_TEXT, SQL_GUIDE

HIBERNATION_SOURCE = ("Its source is leadership's overseeing terrace, so it works only where the asker "
                      "and everyone in this room can read that channel.")
# Usage facts written once for this reader; other lookups send their usage note as is.
NOTES = {
    "read_clash": (
        "Kinds: clan (memberList, levels, leagues), player (compact profile), current_war and cwl_war "
        "(scores and compact rosters), cwl_group (season, clans, rounds.warTags), war_log (items with "
        "results, stars, destruction), battle_log (latest player attacks and defenses), capital_raids "
        "(items without attackLog/defenseLog). current_war, war_log, cwl_group and cwl_war cover only "
        "clans the family record does not collect; for family clans use read_current_war, "
        "read_war_participation or read_cwl_results, and query_bot_data family.war_sides for each war's "
        "result, stars and destruction. Defaults leave out heroes, troops and other bulk fields; "
        "detail=full includes everything. War attacks are at clan.members.attacks. Use resolved tags, "
        "never placeholder values. Ranked history: read_ranked_activity. CWL-only clans need a CWL "
        "cohort first. Clash times look like 20261008T120000.000Z, in UTC; battle_log's before is exclusive."),
    "read_war_participation": "With accounts, members or a cohort and no period, all their recorded wars are read.",
    "read_war_attacks": "",
    "react_to_request": ("Ground each emoji in the same point; judge the whole response. Combinations are "
                         "optional when offered. Avoid previous channel emojis. "
                         "A reaction goes with a line or a GIF, never alone."),
    READ_NAME: "Use manifest kind/ID, never 'latest'; keep filters when paging. XLSX needs exact sheet_name.",
    # The loop has no earlier-step results to point rows_from at, so only queries and written rows remain.
    "prepare_spreadsheet": ("Each replaces the last. Queries write exact data; literal rows "
                            "only for proposals or summaries. Never paste or invent spreadsheet links."),
}
BEFORE = {"query_bot_data": SQL_GUIDE}
ADDED = {"read_hibernation": HIBERNATION_SOURCE, "read_active_hibernation": HIBERNATION_SOURCE}
SEASON = "CWL season key: the league's month, YYYY-MM, or a key a read returned."
REPLACES_CARD_ID = "replaces_card_id"


def _note(name, definition):
    note = NOTES.get(name, definition.usage_note)
    return " ".join(filter(None, (BEFORE.get(name), note.strip(), ADDED.get(name))))


def _rules(schema):
    rules = schema.get(RULES, ())
    if not rules:
        return ""
    if all("report_id" in rule.get("unless", ()) for rule in rules):
        return "Without report_id: " + " ".join(rule_sentences(schema, omit_unless=("report_id",)))
    return " ".join(rule_sentences(schema))


def _season(detail):
    pattern = (detail.get("items", detail)).get("pattern", "")
    return {**detail, "description": SEASON} if pattern.startswith("^\\d{4}-") and "description" not in detail else detail


def _codes(detail):
    """Fields that take ranks or war types as codes say which name in results each code is."""
    if not isinstance(detail, dict):
        return detail
    items = detail.get("items")
    listed = isinstance(items, dict) and "enum" in items
    detail = {key: ({field: _codes(value) for field, value in child.items()} if key == "properties"
                    else _codes(child) if key == "items" and not listed else child)
              for key, child in detail.items()}
    line = codes_line(items["enum"] if listed else detail.get("enum", ()))
    if line:
        detail["description"] = " ".join(filter(None, (detail.get("description"), line)))
    return detail


def _kinds(registry, schema):
    lines = []
    for kind in schema["properties"]["report_kind"]["enum"]:
        inner = original_tool(registry, READ_NAME, {"report_kind": kind}).definition.parameters
        fields = [f"limit up to {detail['maximum']}" if field == "limit" and "maximum" in detail else field
                  for field, detail in inner["properties"].items() if field != "report_id"]
        rules = _rules(inner)
        lines.append(f"{kind}: {', '.join(fields) or 'no filters'}" + (f" ({rules})" if rules else ""))
    return "Kinds and their fields: " + "; ".join(lines) + "."


def model_definition(registry, name):
    tool = registry[name]
    definition = tool.definition
    schema = definition.parameters
    properties = {field: _codes(_season(detail) if field in ("season", "season_keys") else detail)
                  for field, detail in schema.get("properties", {}).items()}
    if tool.action_class in (ActionClass.CHANGE, ActionClass.IRREVERSIBLE):
        properties[REPLACES_CARD_ID] = {
            "type": "integer",
            "minimum": 1,
            "description": "If this revises an earlier proposal, give its open Confirm card ID from the conversation.",
        }
    if name == 'query_bot_data':
        properties['max_rows'] = {key: value for key, value in properties['max_rows'].items() if key != 'default'}
    parts = [definition.description, _note(name, definition), _rules(schema)]
    if tool.finishes_reply_with_text:
        parts.append(FINISH_REPLY_WITH_TEXT)
    if name == READ_NAME:
        parts.append(_kinds(registry, schema))
    parameters = {key: value for key, value in schema.items() if key != RULES}
    if properties or "properties" in schema:
        parameters["properties"] = properties
    return replace(definition, description=" ".join(filter(None, parts)), parameters=parameters, usage_note="")


def model_tools(registry):
    return [model_definition(registry, name) for name in registry]
