"""Real-shaped synthetic Clash evidence, with no live API or member data."""

from copy import deepcopy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.configuration.roles import CORE
from elbow_helper.agent.datasets.clash import read_clash, select_fields, clash_tools
from elbow_helper.agent.engine import budgets
from elbow_helper.agent.engine.steps import PlanRunner
from elbow_helper.agent.engine.tool_call import bound_tool_result, execute_tool
from elbow_helper.agent.models import AgentRequestContext
from elbow_helper.agent.plan.format import capability_list
from elbow_helper.agent.plan.results import model_result, model_view


def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def records(value):
    if isinstance(value, dict):
        if set(value) == {"columns", "rows"}:
            return [records(dict(zip(value["columns"], row))) for row in value["rows"]]
        return {key: records(child) for key, child in value.items()}
    return [records(child) for child in value] if isinstance(value, list) else value


def synthetic_tag(index):
    alphabet = "0289PYLQGRJCUV"
    digits = ""
    for _ in range(6):
        digits = alphabet[index % len(alphabet)] + digits
        index //= len(alphabet)
    return "#PPY" + digits


def icons(kind):
    sizes = {"badges": (("small", 70), ("medium", 200), ("large", 512)),
             "labels": (("small", 64), ("medium", 128))}.get(
                 kind, (("small", 72), ("tiny", 36), ("medium", 288)))
    return {size: f"https://api-assets.clashofclans.com/{kind}/{pixels}/{'s' * 64}.png"
            for size, pixels in sizes}


def synthetic_clan(index=0):
    return {
        "tag": synthetic_tag(1000 + index), "name": f"Synthetic clan {index}",
        "type": "inviteOnly", "description": "Synthetic clan description. " * 6,
        "location": {"id": 32000000, "name": "Synthetic location", "isCountry": False},
        "badgeUrls": icons("badges"), "clanLevel": 30, "clanPoints": 50000,
        "clanBuilderBasePoints": 40000, "clanCapitalPoints": 3000,
        "capitalLeague": {"id": 85000000, "name": "Synthetic capital league"},
        "requiredTrophies": 3000, "warFrequency": "always", "warWinStreak": 10,
        "warWins": 1000, "warLosses": 100, "warTies": 5, "isWarLogPublic": True,
        "warLeague": {"id": 48000000, "name": "Synthetic war league"}, "members": 50,
        "requiredTownhallLevel": 16, "requiredBuilderBaseTrophies": 0,
        "memberList": [{
            "tag": synthetic_tag(index * 50 + member), "name": f"Synthetic {member:03d}",
            "role": ("leader", "coLeader", "admin", "member")[member % 4],
            "townHallLevel": 18 if member % 3 == 0 else 17,
            "expLevel": 250, "league": {"id": 29000022, "name": "Legend League", "iconUrls": icons("leagues")},
            "leagueTier": {"id": 105000031, "name": "Synthetic ranked league"},
            "builderBaseLeague": {"id": 44000030, "name": "Synthetic builder league"},
            "trophies": 5000 + member, "builderBaseTrophies": 4000,
            "clanRank": member + 1, "previousClanRank": member + 1,
            "donations": 100, "donationsReceived": 100,
            "playerHouse": {"elements": [{"type": kind, "id": 82000000 + element}
                                          for element, kind in enumerate(("ground", "walls", "roof", "decoration"))]},
        } for member in range(50)],
        "labels": [{"id": 56000000, "name": "Synthetic label", "iconUrls": icons("labels")}],
        "clanCapital": {"capitalHallLevel": 10, "districts": [
            {"id": 70000000 + district, "name": f"Synthetic district {district}", "districtHallLevel": 5}
            for district in range(9)]},
    }


def synthetic_player(index=0):
    return {
        "tag": synthetic_tag(index), "name": f"Synthetic {index:03d}", "townHallLevel": 18,
        "role": "member", "trophies": 0, "bestTrophies": 6000,
        "league": {"name": "Unranked", "iconUrls": icons("leagues")},
        "leagueTier": {"id": 105000031, "name": "Synthetic ranked league"},
        "clan": {"tag": synthetic_tag(1000), "name": "Synthetic clan", "clanLevel": 30, "badgeUrls": icons("badges")},
        "heroes": [{"name": f"Synthetic hero {hero}", "level": 90, "maxLevel": 100, "village": "home",
                    "equipment": [{"name": f"Synthetic equipment {piece}", "level": 18} for piece in range(3)]}
                   for hero in range(7)],
        "troops": [{"name": f"Synthetic troop {troop}", "level": 10, "maxLevel": 12, "village": "home"}
                   for troop in range(100)],
        "achievements": [{"name": f"Synthetic achievement {item}", "stars": 3, "value": 100000,
                          "target": 100000, "info": "Synthetic achievement description."}
                         for item in range(50)],
    }


def synthetic_war():
    return {"state": "inWar", "teamSize": 50, "attacksPerMember": 2,
            "startTime": "20261021T120000.000Z", "endTime": "20261022T120000.000Z",
            **{side: {"tag": synthetic_tag(1000 + index), "name": f"Synthetic {side}",
                      "clanLevel": 30, "badgeUrls": icons("badges"), "stars": 100, "attacks": 40,
                      "destructionPercentage": 80.5, "members": [
                          {"tag": synthetic_tag(index * 50 + member), "name": f"Synthetic {member:03d}",
                           "townhallLevel": 18, "mapPosition": member + 1, "opponentAttacks": 2,
                           "attacks": [{"attackerTag": synthetic_tag(index * 50 + member),
                                        "defenderTag": synthetic_tag((1 - index) * 50 + member),
                                        "stars": 3, "destructionPercentage": 100.0, "order": member + 1,
                                        "duration": 120} for _ in range(2)]}
                          for member in range(50)]}
               for index, side in enumerate(("clan", "opponent"))}}


def read_context(payloads):
    async def fetch(path, **kwargs):
        tag = next(part.replace("%23", "#") for part in path.split("/") if part.startswith("%23"))
        return SimpleNamespace(ok=True, status=200, payload=deepcopy(payloads[tag]))

    client = SimpleNamespace(configured=True, get=AsyncMock(side_effect=fetch))
    member = SimpleNamespace(id=2, roles=[SimpleNamespace(id=next(iter(CORE)))])
    channel = SimpleNamespace(id=4, permissions_for=lambda _: SimpleNamespace(
        view_channel=True, read_message_history=True))
    guild = SimpleNamespace(id=1, me=member, get_member=lambda _: member,
                            get_channel_or_thread=lambda _: channel)
    channel.guild = guild
    return AgentRequestContext(bot=SimpleNamespace(clash_client=client),
                               guild=guild, member=member, source_message=SimpleNamespace(id=3, channel=channel),
                               account_links=None, message_search=None)


async def measure_evidence():
    clans = [synthetic_clan(index) for index in range(9)]
    context = read_context({clan["tag"]: clan for clan in clans})
    with patch("elbow_helper.agent.datasets.clash.require_evidence_access", AsyncMock()):
        full = await read_clash(context, {"kind": "clan", "tags": [clans[0]["tag"]], "detail": "full"})
        compact = [await read_clash(context, {"kind": "clan", "tags": [clan["tag"]]}) for clan in clans]
        selected = await read_clash(context, {"kind": "clan", "tags": [clan["tag"] for clan in clans],
                                             "fields": ["tag", "name", "memberList.tag", "memberList.townHallLevel"]})
    return {
        "raw_clan": len(encoded(clans[0])),
        "full_evidence_per_clan": len(encoded(model_view(model_result(full)))),
        "compact_evidence_per_clan": len(encoded(model_view(model_result(compact[0])))),
        "nine_separate_compact_reads": sum(len(encoded(model_view(model_result(result)))) for result in compact),
        "nine_clan_th_fields_one_result": len(encoded(model_view(model_result(selected)))),
    }


class CompactClashTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.access = patch("elbow_helper.agent.datasets.clash.require_evidence_access", AsyncMock())
        self.access.start()
        self.addCleanup(self.access.stop)

    async def test_default_rosters_profiles_and_wars_omit_bulk_but_keep_every_row(self):
        for kind, payload in (("clan", synthetic_clan()), ("player", synthetic_player()),
                              ("current_war", synthetic_war()), ("cwl_war", synthetic_war())):
            with self.subTest(kind=kind):
                tag = synthetic_tag(1000)
                context = read_context({tag: payload})
                result = await read_clash(context, {"kind": kind, "tags": [tag]})
                item = result["items"][0]
                self.assertEqual(item["status"], "ok")
                self.assertFalse(result["truncated"])
                self.assertEqual(result["flags"]["status"], "complete")
                self.assertNotIn("iconUrls", encoded(item["data"]))
                self.assertNotIn("heroes", item["data"])
                self.assertNotIn("troops", item["data"])
                if kind == "clan":
                    self.assertEqual(len(item["data"]["memberList"]), 50)
                    self.assertEqual(item["row_counts"]["/memberList"], {"returned_rows": 50, "total_rows": 50})
                    self.assertIn("trophies", item["data"]["memberList"][0])
                    self.assertNotIn("playerHouse", item["data"]["memberList"][0])
                elif kind == "player":
                    self.assertEqual(item["data"]["trophies"], 0)
                    self.assertEqual(item["data"]["leagueTier"], payload["leagueTier"])
                else:
                    for side in ("clan", "opponent"):
                        self.assertEqual(len(item["data"][side]["members"]), 50)
                        self.assertNotIn("attacks", item["data"][side]["members"][0])

    async def test_fields_override_full_and_reuse_raw_read_for_nested_extras(self):
        player = synthetic_player()
        context = read_context({player["tag"]: player})
        await read_clash(context, {"kind": "player", "tags": [player["tag"]]})
        result = await read_clash(context, {"kind": "player", "tags": [player["tag"]], "detail": "full",
                                             "fields": ["name", "heroes.name", "heroes.level"]})
        item = result["items"][0]
        self.assertTrue(item["reused"])
        context.bot.clash_client.get.assert_awaited_once()
        self.assertEqual(item["data"], {"name": player["name"], "heroes": [
            {"name": hero["name"], "level": hero["level"]} for hero in player["heroes"]]})
        self.assertFalse(item["truncated"])
        full = await read_clash(context, {"kind": "player", "tags": [player["tag"]], "detail": "full"})
        self.assertEqual(full["items"][0]["data"], player)
        self.assertIn("equipment", full["items"][0]["data"]["heroes"][0])

    def test_parent_selection_empty_lists_missing_fields_and_no_mutation(self):
        payload = synthetic_player()
        before = deepcopy(payload)
        for fields in (["heroes.level", "heroes"], ["heroes", "heroes.level"]):
            self.assertEqual(select_fields(payload, fields), {"heroes": payload["heroes"]})
        self.assertEqual(select_fields({"members": []}, ["members.name", "missing"]), {"members": []})
        self.assertEqual(payload, before)
        war = synthetic_war()
        self.assertEqual(select_fields(war, ["clan.members.attacks.stars"])["clan"]["members"][0],
                         {"attacks": [{"stars": 3}, {"stars": 3}]})

    async def test_field_validation_precedes_io_and_is_visible_in_catalogue(self):
        player = synthetic_player()
        context = read_context({player["tag"]: player})
        for fields in ([], "heroes", [1], [""], ["heroes.*"], ["/heroes"], ["heroes..name"],
                       ["x" * 161], ["name"] * 41):
            with self.subTest(fields=fields):
                result = await read_clash(context, {"kind": "player", "tags": [player["tag"]], "fields": fields})
                self.assertEqual(result["cause"], "invalid_arguments")
        context.bot.clash_client.get.assert_not_awaited()
        catalogue = capability_list({tool.definition.name: tool for tool in clash_tools()})
        self.assertIn("fields:[str(length>=1)]", catalogue)
        self.assertIn("fields overrides detail", catalogue)
        self.assertIn("CWL-only clans need a seasonal CWL cohort first", catalogue)

    async def test_nine_clan_count_survives_actual_one_round_evidence_path(self):
        clans = [synthetic_clan(index) for index in range(9)]
        context = read_context({clan["tag"]: clan for clan in clans})
        registry = {tool.definition.name: tool for tool in clash_tools()}
        for selected in (False, True):
            with self.subTest(selected=selected):
                steps = [{"id": f"clan-{index}", "capability": "read_clash", "depends_on": [],
                          "arguments": {"kind": "clan", "tags": [clan["tag"]]}}
                         for index, clan in enumerate(clans)]
                if selected:
                    steps = [{"id": "family", "capability": "read_clash", "depends_on": [],
                              "arguments": {"kind": "clan", "tags": [clan["tag"] for clan in clans],
                                            "fields": ["tag", "name", "memberList.tag", "memberList.townHallLevel"]}}]
                runner = PlanRunner(service=SimpleNamespace(execute_tool=execute_tool), context=context,
                                    registry=registry, command_capabilities={}, rounder=SimpleNamespace(social_route=False),
                                    budget=budgets.ContextBudget(128000, 8000, 2000))
                results = await runner.run({"goal": "Count family TH18 accounts", "effort": "high",
                                            "output": "text", "steps": steps})
                items = []
                for view in runner.model_results_for(results).values():
                    self.assertLess(len(encoded(view)), budgets.MAX_TOOL_RESULT_CHARACTERS)
                    view = records(view)
                    self.assertFalse(view["flags"]["truncated"])
                    self.assertEqual(view["flags"]["status"], "complete")
                    items.extend(view["items"])
                self.assertEqual(len(items), 9)
                members = [member for item in items for member in item["data"]["memberList"]]
                self.assertEqual(len(members), 450)
                self.assertEqual(sum(member["townHallLevel"] == 18 for member in members), 9 * 17)
                self.assertLess(runner.evidence_characters, budgets.MAX_EVIDENCE_CHARACTERS * 0.75)

    async def test_size_fallback_keeps_nine_rosters_and_names_exact_omitted_fields(self):
        clans = [synthetic_clan(index) for index in range(9)]
        context = read_context({clan["tag"]: clan for clan in clans})
        result = await read_clash(context, {"kind": "clan", "tags": [clan["tag"] for clan in clans]})
        content = bound_tool_result(encoded(model_view(model_result(result))), budgets.MAX_TOOL_RESULT_CHARACTERS)
        view = records(json.loads(content))
        self.assertLessEqual(len(content), budgets.MAX_TOOL_RESULT_CHARACTERS)
        self.assertTrue(view["flags"]["truncated"])
        self.assertEqual(view["read_statuses"], {clan["tag"]: "ok" for clan in clans})
        self.assertEqual(len(view["items"]), 9)
        for item in view["items"]:
            self.assertEqual(item["status"], "ok")
            self.assertEqual(len(item["data"]["memberList"]), 50)
            self.assertIn("/memberList/*/name", item["omitted_fields"])
            self.assertEqual(item["row_counts"]["/memberList"], {"returned_rows": 50, "total_rows": 50})

    async def test_small_page_distinguishes_omitted_successes_from_failed_reads(self):
        players = [synthetic_player(index) for index in range(12)]
        context = read_context({player["tag"]: player for player in players})
        result = await read_clash(context, {"kind": "player", "tags": [player["tag"] for player in players]})
        result["items"][-1] = {"tag": players[-1]["tag"], "status": "not_found", "http_status": 404}
        content = bound_tool_result(encoded(model_view(model_result(result))), 4000)
        view = records(json.loads(content))
        self.assertLessEqual(len(content), 4000)
        self.assertEqual(view["read_statuses"][players[-1]["tag"]], "not_found")
        self.assertTrue(view["next_tags"])
        for omitted in view["row_counts"]:
            self.assertEqual(omitted["status"], view["read_statuses"][omitted["tag"]])
            self.assertTrue(all(count["returned_rows"] == 0 for count in omitted["collections"].values()))
        self.assertIn("/items/*/data", view["omitted_fields"])

    async def test_full_projection_size_cut_reports_fields_and_collection_totals(self):
        player = synthetic_player()
        context = read_context({player["tag"]: player})
        result = await read_clash(context, {"kind": "player", "tags": [player["tag"]], "detail": "full"})
        content = bound_tool_result(encoded(model_view(model_result(result))), 4000)
        view = records(json.loads(content))
        item = view["items"][0]
        self.assertEqual(item["status"], "ok")
        self.assertEqual(view["read_statuses"], {player["tag"]: "ok"})
        for path, count in (("/heroes", 7), ("/troops", 100), ("/achievements", 50)):
            self.assertIn(path, item["omitted_fields"])
            self.assertEqual(item["row_counts"][path], {"returned_rows": 0, "total_rows": count})

    async def test_maximum_player_batch_retains_statuses_even_when_metadata_needs_compacting(self):
        players = [synthetic_player(index) for index in range(200)]
        context = read_context({player["tag"]: player for player in players})
        result = await read_clash(context, {"kind": "player", "tags": [player["tag"] for player in players]})
        content = bound_tool_result(encoded(model_view(model_result(result))), budgets.MAX_TOOL_RESULT_CHARACTERS)
        view = records(json.loads(content))
        self.assertLessEqual(len(content), budgets.MAX_TOOL_RESULT_CHARACTERS)
        self.assertEqual(view["read_statuses"], {player["tag"]: "ok" for player in players})
        self.assertEqual(set(view["next_tags"]), {player["tag"] for player in players})
        self.assertEqual(view["omitted_collection_counts"]["/"], {"returned_rows": 0, "total_rows": 200})

    async def test_real_shaped_measurement_stays_well_inside_existing_caps(self):
        sizes = await measure_evidence()
        self.assertGreater(sizes["raw_clan"], 45000)
        self.assertLess(sizes["raw_clan"], 70000)
        self.assertLess(sizes["compact_evidence_per_clan"], sizes["full_evidence_per_clan"] * 0.15)
        self.assertLess(sizes["nine_separate_compact_reads"], budgets.MAX_EVIDENCE_CHARACTERS * 0.75)
        self.assertLess(sizes["nine_clan_th_fields_one_result"], budgets.MAX_TOOL_RESULT_CHARACTERS * 0.75)


if __name__ == "__main__":
    unittest.main()
