import unittest
from unittest.mock import AsyncMock, MagicMock

from elbow_helper.configuration.clans import CLANS
from elbow_helper.features.rosters.models import Roster, RosterLayout, RosterMember
from elbow_helper.features.rosters.services.ordering import RosterOrder


def member(tag: str, townhall: int) -> RosterMember:
    return RosterMember(tag, 1, tag, "BE", townhall, 0)


def roster(clan_code: str) -> Roster:
    return Roster(
        id=1, guild_id=1, name="Test", clan_code=clan_code, role_id=None, max_members=50,
        min_townhall=None, buttons_hidden=False, status="open", schedule_enabled=False,
        schedule_utc_offset=None, open_day=None, open_time=None, close_day=None,
        close_time=None, one_off_open_ts=None, one_off_close_ts=None, reset_on_open=False,
        active_cycle_id=1, last_open_cycle_key=None, last_close_cycle_key=None,
    )


def standing(league: int, trophies: int, clan_rank: int | None = None) -> dict[str, int | None]:
    return {"league_id": league, "trophies": trophies, "clan_rank": clan_rank}


class RosterOrderTests(unittest.IsolatedAsyncioTestCase):
    async def test_war_order_ranks_league_then_trophies_then_clan_rank_then_townhall(self) -> None:
        # Members arrive in Town Hall order, as the repository returns them.
        members = [member(tag, th) for tag, th in
                   (("#A", 19), ("#B", 19), ("#C", 18), ("#D", 17), ("#E", 16), ("#F", 15))]
        reads = MagicMock(standings=AsyncMock(return_value={
            "#A": standing(105000033, 0),
            "#B": standing(105000033, 0),
            "#C": standing(105000033, 0, 9),
            "#D": standing(105000033, 0, 4),
            "#E": standing(105000035, 10),
            "#F": standing(105000033, 500),
        }))
        order = RosterOrder(reads)
        clan_code = next(iter(CLANS))

        war = await order.apply(members, RosterLayout(sort_order="war"), roster(clan_code))
        townhall = await order.apply(members, RosterLayout(), roster(clan_code))

        self.assertEqual([row.player_tag for row in war], ["#E", "#F", "#D", "#C", "#A", "#B"])
        self.assertEqual(townhall, members)
        reads.standings.assert_awaited_with(
            ["#A", "#B", "#C", "#D", "#E", "#F"], clan_tag=CLANS[clan_code].tag,
        )

    async def test_roster_without_a_real_clan_keeps_townhall_order_in_ties(self) -> None:
        members = [member("#A", 19), member("#B", 18)]
        reads = MagicMock(standings=AsyncMock(return_value={
            "#A": standing(105000033, 0),
            "#B": standing(105000033, 0),
        }))

        war = await RosterOrder(reads).apply(
            members, RosterLayout(sort_order="war"), roster("FAMILY"),
        )

        self.assertEqual([row.player_tag for row in war], ["#A", "#B"])
        reads.standings.assert_awaited_with(["#A", "#B"], clan_tag=None)


if __name__ == "__main__":
    unittest.main()
