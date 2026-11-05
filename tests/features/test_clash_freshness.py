from __future__ import annotations

import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from elbow_helper.configuration.clans import CLANS, CLAN_TAGS
from elbow_helper.domain.player_tags import encode_clash_tag
from elbow_helper.features.account_links.cog import AccountLinks
from elbow_helper.features.account_links.evidence import refresh_account_locations
from elbow_helper.features.clan_reporting.cog import ClanReporting
from elbow_helper.features.cwl.transfer_hub import CwlTransferHubMixin
from elbow_helper.features.diagnostics.cog import DebugCog
from elbow_helper.features.rosters.models import LinkedAccount, RosterMember
from elbow_helper.data.collectors.demand import SECOND_US
from elbow_helper.data.store import now_us
from elbow_helper.features.rosters.services.profiles import (
    PROFILE_MAX_AGE_SECONDS, RosterProfileService, fetch_account_profiles,
)
from elbow_helper.infrastructure.clash import ClashClient

from tests.core.test_clash_infrastructure import _FakeResponse, _FakeSession
from tests.features.profile_source import profile_demand


class ClashFreshnessTests(unittest.IsolatedAsyncioTestCase):
    def client(self, responses):
        client = ClashClient("token", default_attempts=1, default_backoff_seconds=0)
        session = _FakeSession(responses)
        client._session = session
        self.addAsyncCleanup(client.close)
        return client, session

    def profile(self, code):
        return {"tag": "#2PP", "name": "Player", "townHallLevel": 18,
                "clan": {"tag": CLANS[code].tag, "name": CLANS[code].name}}

    async def test_fresh_profiles_report_a_move_or_failure_instead_of_a_kept_location(self):
        for status in (200, 503):
            with self.subTest(status=status):
                client, session = self.client([
                    _FakeResponse(200, self.profile("BE4"), headers={"Cache-Control": "max-age=60"}),
                    _FakeResponse(status, self.profile("BEH")),
                ])
                profiles_source = profile_demand(client)
                account = LinkedAccount("#2PP", "Player", "BE4", 18)
                before, _ = await fetch_account_profiles([account], profiles_source)
                kept, _ = await fetch_account_profiles([account], profiles_source)
                self.assertEqual(before, kept)
                self.assertEqual(len(session.urls), 1)
                profiles, failed = await fetch_account_profiles([account], profiles_source, fresh=True)
                if status == 200:
                    self.assertEqual(profiles[account.player_tag].clan_code, "BEH")
                    self.assertFalse(failed)
                else:
                    self.assertEqual(profiles, {})
                    self.assertEqual(failed, {account.player_tag})
                self.assertEqual(len(session.urls), 2)

    async def test_roster_profiles_ask_for_at_most_a_minute_old_and_refresh_always_fetches(self):
        asked = []

        async def player(tag, *, max_age=None, **_):
            asked.append(max_age)
            return SimpleNamespace(payload=self.profile("BEH"), status="ok", failure=None)

        source = SimpleNamespace(configured=True, player=player)
        account = LinkedAccount("#2PP", "Player", "BE4", 18)
        await fetch_account_profiles([account], source)
        await fetch_account_profiles([account], source, fresh=True)
        self.assertEqual(asked, [PROFILE_MAX_AGE_SECONDS, 0])
        self.assertEqual(PROFILE_MAX_AGE_SECONDS, 60)

    async def test_roster_profile_older_than_a_minute_is_fetched_again(self):
        client, session = self.client([
            _FakeResponse(200, self.profile("BE4"), headers={"Cache-Control": "max-age=3600"}),
            _FakeResponse(200, self.profile("BEH")),
        ])
        source = profile_demand(client)
        account = LinkedAccount("#2PP", "Player", "BE4", 18)
        await source.player("#2PP")
        with patch("elbow_helper.data.collectors.demand.now_us", return_value=now_us() + 61 * SECOND_US):
            profiles, failed = await fetch_account_profiles([account], source)
        self.assertEqual(profiles[account.player_tag].clan_code, "BEH")
        self.assertFalse(failed)
        self.assertEqual(len(session.urls), 2)

    async def test_transfer_hub_checks_the_member_after_joining_the_new_clan(self):
        client, session = self.client([
            _FakeResponse(200, self.profile("BE4"), headers={"Cache-Control": "max-age=60"}),
            _FakeResponse(200, self.profile("BEH")),
        ])
        hub = CwlTransferHubMixin()
        hub.profiles = profile_demand(client)
        await hub.profiles.player("#2PP")
        assignments = {"BEH": [RosterMember("#2PP", 10, "Player", "BE4", 18, 1)]}
        hub._member_cwl_assignments = AsyncMock(return_value=assignments)
        hub._build_member_cwl_embed = MagicMock(return_value="embed")
        self.assertEqual(await hub.member_cwl_information(1, 10), (None, "embed"))
        profiles = hub._build_member_cwl_embed.call_args.args[1]
        self.assertEqual(profiles["#2PP"].clan_code, "BEH")
        self.assertEqual(len(session.urls), 2)

    async def test_roster_refresh_persists_the_new_clan_instead_of_the_kept_one(self):
        client, session = self.client([
            _FakeResponse(200, self.profile("BE4"), headers={"Cache-Control": "max-age=60"}),
            _FakeResponse(200, self.profile("BEH")),
        ])
        profiles_source = profile_demand(client)
        await profiles_source.player("#2PP")
        member = RosterMember("#2PP", 10, "Player", "BE4", 18, 1)
        repository = SimpleNamespace(refresh_member_snapshots=MagicMock(), list_members=MagicMock(return_value=[member]))
        service = RosterProfileService(repository, profiles_source)
        await service.refresh(SimpleNamespace(id=1, active_cycle_id=2), [member])
        snapshots = repository.refresh_member_snapshots.call_args.args[2]
        self.assertEqual(snapshots["#2PP"]["clan_code"], "BEH")
        self.assertEqual(len(session.urls), 2)

    async def test_explicit_location_refresh_keeps_failure_distinct_from_leaving(self):
        for status in (200, 503):
            with self.subTest(status=status):
                client, session = self.client([
                    _FakeResponse(200, self.profile("BE4"), headers={"Cache-Control": "max-age=60"}),
                    _FakeResponse(status, {"tag": "#2PP", "name": "Player"}),
                ])
                profiles_source = profile_demand(client)
                await profiles_source.player("#2PP")
                row = dict(player_tag="#2PP", player_name="Player", clan_tag=None,
                           clan_code=None, clan_name=None, clan_role=None, in_clan=None,
                           location_status="unknown", checked_at=None)
                result, = await refresh_account_locations(SimpleNamespace(profiles=profiles_source), [row])
                self.assertEqual(result["location_status"], "refreshed" if status == 200 else "refresh_failed")
                self.assertEqual(result["in_clan"], False if status == 200 else None)
                self.assertEqual(len(session.urls), 2)

    async def test_manual_elder_board_refresh_passes_fresh_to_the_snapshot(self):
        links = SimpleNamespace(
            _snapshot_lock=asyncio.Lock(), _rebuild_snapshots=AsyncMock(),
            _prune_departed_suggestions=AsyncMock(), _process_unlinked_players=AsyncMock(),
            _snapshot_ready=asyncio.Event(), refresh_linked_boards=AsyncMock(),
        )
        links.refresh_now = lambda **kwargs: AccountLinks.refresh_now(links, **kwargs)
        reporting = SimpleNamespace(account_links=links, _update_missing_elder_message=AsyncMock(return_value=True))
        self.assertTrue(await ClanReporting.refresh_missing_elder_board_from_accounts(reporting, "BEH"))
        links._rebuild_snapshots.assert_awaited_once_with(fresh=True)
        links.refresh_linked_boards.assert_not_awaited()
        links._rebuild_snapshots.reset_mock()
        await AccountLinks.refresh_now(links)
        links._rebuild_snapshots.assert_awaited_once_with(fresh=False)

    async def test_clan_members_refresh_reads_the_record_without_a_clash_request(self):
        client, session = self.client([])
        links = AccountLinks(SimpleNamespace(), profile_demand(client))
        links.membership_reads = SimpleNamespace(rosters=AsyncMock(return_value={"rows": [
            {"members": [], "badge_url": None}]}))
        self.assertEqual(await links._fetch_clan_members("BEH", fresh=True), [])
        links.membership_reads.rosters.assert_awaited_once_with(clans=["BEH"])
        self.assertEqual(session.urls, [])


    async def test_connection_check_reports_a_request_instead_of_cached_success(self):
        client, session = self.client([
            _FakeResponse(200, {}, headers={"Cache-Control": "max-age=120"}),
            _FakeResponse(503, {}),
        ])
        clan, tag = next(iter(CLAN_TAGS.items()))
        await client.get(f"/clans/{encode_clash_tag(tag)}/currentwar")
        diagnostics = object.__new__(DebugCog)
        diagnostics.clash_client = client
        result = await diagnostics.check_clash_connection(clan)
        self.assertEqual(result.response.status, 503)
        self.assertEqual(result.response.attempts, 1)
        self.assertIsNotNone(result.response.latency_ms)
        self.assertEqual(len(session.urls), 2)

    async def test_linking_identity_lookup_can_use_a_kept_profile(self):
        client, session = self.client([
            _FakeResponse(200, self.profile("BEH"), headers={"Cache-Control": "max-age=60"}),
        ])
        links = object.__new__(AccountLinks)
        links.profiles = profile_demand(client)
        await links.profiles.player("#2PP")
        self.assertEqual(await links.lookup_players(["#2PP"]), [{"player_tag": "#2PP", "player_name": "Player"}])
        self.assertEqual(len(session.urls), 1)
