"""Scripted engine cost comparison for the archived workloads' failure modes.

PYTHONPATH=tests python -m features.agent.engine.efficiency_scenarios [RUN_DIRECTORY]

Models, identities and results are synthetic. Optional archived schema/knowledge
text supplies a realistic shared prefix, read-only. Token totals use the same
character heuristic on both sides; they are not live provider measurements.
The before scripts reproduce serial/probing plans with the old emission policy;
after scripts use the now-documented return paths and batched reads. This tests
execution cost and completeness, not a model's ability to choose those plans.
"""

import asyncio
from dataclasses import replace
import json
from pathlib import Path
import re
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from elbow_helper.features.agent.engine.registry import build_agent_tools
from elbow_helper.features.agent.engine.service import AgentService
from elbow_helper.features.agent.knowledge.store import KnowledgePrompt
from features.agent.engine.cost_scenarios import MeasuredModel
from features.agent.engine.test_agent_plan_flow import _context
from features.agent.engine.test_efficiency import planned, scores, step


def ref(identity, *path):
    return {"step": identity, "path": list(path)}


def workloads():
    who = step("who", "find_discord_members", {"query": "Synthetic"})
    cohort = step("cohort", "read_discord_members", {"joined_after": "2026-10-01T00:00:00Z"})
    links = step("links", "read_linked_accounts", {"member_ids": ref("who", "members", "*", "member_id")})
    profiles = step("profiles", "read_clash", {"kind": "player", "tags": ref("links", "accounts", "*", "player_tag")})
    performance = step("performance", "read_cwl_performance", {"player_tag": ref("links", "accounts", 0, "player_tag")})
    rules = step("rules", "search_approved_knowledge", {"titles": ["Synthetic entry requirements"]})
    logs = [step(f"log_{index}", "read_discord_channel_history", {
        "channel_id": 61, "after": f"2026-10-{index + 1:02}T00:00:00Z",
    }) for index in range(4)]
    notes = [step(f"notes_{index}", "read_accessible_support_tickets", {"channel_id": 61 + index})
             for index in range(5)]
    wars = step("wars", "query_bot_data", {"sql": "SELECT war_id FROM health.wars WHERE clan_code='BE1'"})
    ass = step("ass", "cwl_ass_scores", {"clan_code": "BE1", "war_ids": ref("wars", "rows", "*", "war_id")})
    return {
        "accounts": ("List Synthetic's linked accounts", [who, links], [who, links], "One linked account: #P0Y."),
        "recruitment": ("Summarise recruitment trial updates", logs, [dict(logs[0], arguments={
            **logs[0]["arguments"], "pages": 4})], "Four trial updates are recorded."),
        "standing": ("Check Synthetic's recorded gameplay standing", [who, links, profiles, performance, rules],
                     [who, links, profiles, performance, rules], "Seven attacks, all triples; competitive readiness still requires review."),
        "refusal": ("Share recruiter notes about an applicant", notes, notes[:1], "Recruiter notes are for Recruiters."),
        "be4_cohort": ("Check this month's server joiners against the entry requirements", [cohort, rules,
                       dict(links, arguments={"member_ids": ref("cohort", "members", "*", "member_id")}), profiles, performance],
                       [cohort, rules, dict(links, arguments={"member_ids": ref("cohort", "members", "*", "member_id")}),
                        profiles, performance], "One linked account meets progression requirements; gameplay review remains necessary."),
        "clan_ass": ("What is the ASS score for BE1 from this past month?", [wars, ass, rules],
                     [wars, ass, rules], "BE1 ASS\n```text\n" + scores()["ranking"]["text"] + "\n```"),
    }


def prefix(directory):
    if not directory:
        return "synthetic.accounts: member_id INT, player_tag TEXT", "Synthetic entry requirements: gameplay review required."
    paths = list(Path(directory).glob("*-1562645888264830976.json"))
    if len(paths) != 1:
        raise ValueError("Expected the archived BE4 run")
    record = json.loads(paths[0].read_text(encoding="utf-8-sig"))
    system = next(item["system_prompt"] for item in record["model_sessions"] if item.get("tools"))
    return tuple(re.search(f"<{name}>\\n(.*?)\\n</{name}>", system, re.S)[1]
                 for name in ("data_guide", "community_knowledge"))


async def measure(directory=None):
    schema, knowledge = prefix(directory)
    data = {
        "find_discord_members": {"members": [{"member_id": 1}], "status": "resolved"},
        "read_discord_members": {"members": [{"member_id": 1}], "next_offset": None},
        "read_linked_accounts": {"accounts": [{"member_id": 1, "player_tag": "#P0Y", "player_name": "Synthetic"}]},
        "read_clash": {"source": "clash_api", "kind": "player", "items": [{"tag": "#P0Y", "status": "ok", "data": {"townHallLevel": 18}}]},
        "read_cwl_performance": {"accounts": [{"player_tag": "#P0Y", "attacks": 7, "stars": 21}]},
        "search_approved_knowledge": {"sections": [{"title": "Synthetic entry requirements", "body": "TH18 and gameplay review required."}]},
        "read_accessible_support_tickets": {"error": "This needs recruiter access.", "required_access": ["recruiter"]},
        "query_bot_data": {"rows": [{"war_id": "CWL:synthetic"}], "row_count": 1},
        "cwl_ass_scores": scores(),
    }
    async def history(context, arguments):
        context.state.source_channels.add(61)
        return {"channel_id": 61, "messages": [{"message_id": index + 1, "channel_id": 61,
                "author_id": 1, "content": "Synthetic trial update",
                "source": f"https://discord.com/channels/5/61/{index + 1}"}
                for index in range(arguments.get("pages", 1))]}
    registry = build_agent_tools()
    for name, payload in data.items():
        registry[name] = replace(registry[name], handler=AsyncMock(return_value=payload))
    registry["read_discord_channel_history"] = replace(registry["read_discord_channel_history"], handler=history)
    guide = SimpleNamespace(for_levels=lambda _: schema)
    store = SimpleNamespace(load=lambda: SimpleNamespace(prompt=lambda _: KnowledgePrompt(knowledge, frozenset(), 0)))
    measurements = {}
    for name, (question, serial, grouped, final) in workloads().items():
        measurements[name] = {}
        answers = []
        for revised in (False, True):
            steps = [planned(*grouped)] if revised else [planned(item) for item in serial]
            model = MeasuredModel([*steps, final])
            context = replace(_context(), knowledge_store=store)
            with patch("elbow_helper.features.agent.engine.service.build_agent_tools", return_value=registry):
                if revised:
                    answer = await AgentService(model, data_guide=guide).answer(question=question, local_context="", context=context)
                else:
                    with (patch("elbow_helper.features.agent.engine.flow.access_refused", return_value=False),
                          patch("elbow_helper.features.agent.engine.evidence_delta.EvidenceDelta.prepare", side_effect=lambda results, *_: (results, True))):
                        answer = await AgentService(model, data_guide=guide).answer(question=question, local_context="", context=context)
            answers.append(answer)
            rows = [*model.router.rounds, *model.rounds]
            measurements[name]["after" if revised else "before"] = {
                "rounds": len(rows), "prompt_tokens_estimated": sum(row["input"] for row in rows),
                "completion_tokens_estimated": sum(row["output"] for row in rows),
                "tool_calls": len(context.state.evidence),
            }
        assert answers[0] == answers[1], name
    return {"method": "scripted plans; synthetic data; shared prefix; heuristic tokens", "cases": measurements}


if __name__ == "__main__":
    print(json.dumps(asyncio.run(measure(sys.argv[1] if len(sys.argv) > 1 else None)), indent=2))
