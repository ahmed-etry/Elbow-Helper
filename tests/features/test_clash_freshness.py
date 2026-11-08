from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock

from elbow_helper.configuration.clans import CLANS, CLAN_TAGS
from elbow_helper.domain.player_tags import encode_clash_tag
from elbow_helper.features.cwl.transfer_hub import CwlTransferHubMixin
from elbow_helper.features.diagnostics.cog import DebugCog
from elbow_helper.features.rosters.models import LinkedAccount, RosterMember
from elbow_helper.features.rosters.services.profiles import RosterProfileService, fetch_account_profiles
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
