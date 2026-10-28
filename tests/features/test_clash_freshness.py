from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock

from elbow_helper.configuration.clans import CLANS, CLAN_TAGS
from elbow_helper.domain.player_tags import encode_clash_tag
from elbow_helper.features.account_links.cog import AccountLinks
from elbow_helper.features.account_links.evidence import refresh_account_locations
from elbow_helper.features.clan_reporting.cog import ClanReporting
from elbow_helper.features.cwl.config import CWL_CLAN_TAGS
from elbow_helper.features.cwl.transfer_hub import CwlTransferHubMixin
from elbow_helper.features.cwl.transfers import CwlTransferMixin
from elbow_helper.features.diagnostics.cog import DebugCog
from elbow_helper.features.rosters.models import LinkedAccount, RosterMember
from elbow_helper.features.rosters.services.profiles import RosterProfileService, fetch_account_profiles
from elbow_helper.infrastructure.clash import ClashClient

from tests.core.test_clash_infrastructure import _FakeResponse, _FakeSession


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

    async def test_fresh_profiles_report_a_move_or_failure_instead_of_cached_location(self):
        for status in (200, 503):
            with self.subTest(status=status):
                client, session = self.client([
                    _FakeResponse(200, self.profile("BE4"), headers={"Cache-Control": "max-age=60"}),
                    _FakeResponse(status, self.profile("BEH")),
                ])
                account = LinkedAccount("#2PP", "Player", "BE4", 18)
                before, _ = await fetch_account_profiles([account], client)
                cached, _ = await fetch_account_profiles([account], client)
                self.assertEqual(before, cached)
                self.assertEqual(len(session.urls), 1)
                profiles, failed = await fetch_account_profiles([account], client, fresh=True)
                if status == 200:
                    self.assertEqual(profiles[account.player_tag].clan_code, "BEH")
                    self.assertFalse(failed)
                else:
                    self.assertEqual(profiles, {})
                    self.assertEqual(failed, {account.player_tag})
                self.assertEqual(len(session.urls), 2)

    async def test_transfer_hub_checks_the_member_after_joining_the_new_clan(self):
        client, session = self.client([
            _FakeResponse(200, self.profile("BE4"), headers={"Cache-Control": "max-age=60"}),
            _FakeResponse(200, self.profile("BEH")),
        ])
        await client.get("/players/%232PP")
        hub = CwlTransferHubMixin()
        hub.clash_client = client
        assignments = {"BEH": [RosterMember("#2PP", 10, "Player", "BE4", 18, 1)]}
        hub._member_cwl_assignments = AsyncMock(return_value=assignments)
        hub._build_member_cwl_embed = MagicMock(return_value="embed")
        self.assertEqual(await hub.member_cwl_information(1, 10), (None, "embed"))
        profiles = hub._build_member_cwl_embed.call_args.args[1]
        self.assertEqual(profiles["#2PP"].clan_code, "BEH")
        self.assertEqual(len(session.urls), 2)

    async def test_roster_refresh_persists_the_new_clan_instead_of_the_cached_one(self):
        client, session = self.client([
            _FakeResponse(200, self.profile("BE4"), headers={"Cache-Control": "max-age=60"}),
            _FakeResponse(200, self.profile("BEH")),
        ])
        await client.get("/players/%232PP")
        member = RosterMember("#2PP", 10, "Player", "BE4", 18, 1)
        repository = SimpleNamespace(refresh_member_snapshots=MagicMock(), list_members=MagicMock(return_value=[member]))
        service = RosterProfileService(repository, client)
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
                await client.get("/players/%232PP")
                row = dict(player_tag="#2PP", player_name="Player", clan_tag=None,
                           clan_code=None, clan_name=None, clan_role=None, in_clan=None,
                           location_status="unknown", checked_at=None)
                result, = await refresh_account_locations(SimpleNamespace(clash_client=client), [row])
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

    async def test_clan_members_refresh_bypasses_the_cached_roster(self):
        client, session = self.client([
            _FakeResponse(200, {"members": 1, "memberList": [{"tag": "#2PP"}]},
                          headers={"Cache-Control": "max-age=120"}),
            _FakeResponse(200, {"members": 0, "memberList": []}),
        ])
        links = object.__new__(AccountLinks)
        links.clash_client = client
        links._clan_fetch_warning_state = {}
        links._clan_badge_urls = {}
        before = await links._fetch_clan_members("BEH")
        self.assertEqual(len(before), 1)
        self.assertEqual(await links._fetch_clan_members("BEH", fresh=True), [])
        self.assertEqual(len(session.urls), 2)

    async def test_cwl_spin_recheck_does_not_use_a_cached_group(self):
        for status in (200, 503):
            with self.subTest(status=status):
                client, session = self.client([
                    _FakeResponse(200, {"state": "inWar"}, headers={"Cache-Control": "max-age=120"}),
                    _FakeResponse(status, {"state": "searching", "rounds": []}),
                ])
                await client.get(f"/clans/{encode_clash_tag(CWL_CLAN_TAGS['BEH'])}/currentwar/leaguegroup")
                workflow = CwlTransferMixin()
                workflow.clash_client = client
                workflow._get_league_wars = AsyncMock(return_value=[])
                self.assertEqual(await workflow._cwl_spin_statuses(
                    {"BEH"}, datetime(2026, 10, 1, tzinfo=timezone.utc),
                ), (set(), set() if status == 200 else {"BEH"}))
                workflow._get_league_wars.assert_not_awaited()
                self.assertEqual(len(session.urls), 2)

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

    async def test_linking_identity_lookup_can_use_a_cached_profile(self):
        client, session = self.client([
            _FakeResponse(200, self.profile("BEH"), headers={"Cache-Control": "max-age=60"}),
        ])
        await client.get("/players/%232PP")
        links = object.__new__(AccountLinks)
        links.clash_client = client
        self.assertEqual(await links.lookup_players(["#2PP"]), [{"player_tag": "#2PP", "player_name": "Player"}])
        self.assertEqual(len(session.urls), 1)
