"""Test helpers for agent reads whose regular wars come from the family record.

Real record reads are covered in ``test_record_wars``. Tests about joins
around them (hibernation, trials, cohorts) serve synthetic regular wars as
the record's war reads return them, shaped by the record module itself.
"""

from dataclasses import replace
from itertools import count
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from elbow_helper.agent.families import record_wars
from elbow_helper.configuration.clans import CLANS

SECOND = 1_000_000
WAR_IDS = count(1)


def with_record(context, reads=None):
    """The context with the bot's family record war reads."""
    return replace(context, bot=SimpleNamespace(war_reads=reads if reads is not None else SimpleNamespace()))


def regular_war(clan_code, end_ts, lineup, *, opponent="#Q2", start_ts=None, attacks_per_member=2,
                type_basis="war_log"):
    """One ended regular war as the record's reads return it, with its lineup rows.

    ``lineup`` maps an account tag to (attacks used, stars of each stored attack).
    """
    tag = CLANS[clan_code].tag
    view = {"war_id": next(WAR_IDS), "clan_tag": tag, "state": "warEnded",
            "type_basis": type_basis, "start_at": None if start_ts is None else start_ts * SECOND,
            "end_at": end_ts * SECOND, "legacy_ids": [], "opponent": {"clan_tag": opponent},
            "coverage": {"own": {"members": len(lineup), "team_size": len(lineup), "lineup_complete": True,
                                 "attacks_complete": True}}}
    rows = [{"player_tag": player, "player_name": player, "attacks_expected": attacks_per_member,
             "attacks_used": used, "missed": attacks_per_member - used, "attacks_measured": len(stars),
             "stars": sum(stars), "triples": sum(star == 3 for star in stars),
             "destruction": sum(100.0 if star == 3 else 40.0 for star in stars)}
            for player, (used, stars) in sorted(lineup.items())]
    return view, rows


def regular_id(war):
    """The ID the record's regular wars give a war."""
    return record_wars.war_id(war[0])


def regular_wars_from(*wars):
    """Serve the record's regular wars from these synthetic wars, selected as the record selects them."""
    async def serve(context, *, clan_codes, after_ts=None, before_ts=None, war_ids=(), latest_completed=None):
        selection = SimpleNamespace(wars={}, rows={}, coverage=[], friendly=0, at=record_wars.read_core.now_us())
        tags = {CLANS[code].tag for code in clan_codes}
        for view, rows in wars:
            end = view["end_at"]
            if view["clan_tag"] not in tags or after_ts is not None and end < after_ts * SECOND or (
                    before_ts is not None and end >= before_ts * SECOND):
                continue
            selection.wars[view["war_id"], view["clan_tag"]] = view
            selection.rows[view["war_id"], view["clan_tag"]] = rows
        if war_ids:
            return record_wars.snapshot(selection, requested=set(war_ids))
        if latest_completed:
            return record_wars.snapshot(selection, latest=latest_completed)
        return record_wars.snapshot(selection, window=True)
    return patch.object(record_wars, "regular_wars", AsyncMock(side_effect=serve))
