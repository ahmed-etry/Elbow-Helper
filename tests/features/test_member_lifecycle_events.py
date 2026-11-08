"""Lifecycle keeps join sources, leave reasons and its reports; times come from the family record."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
from tests.temp_directory import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.configuration.channels import OVERSEEING_TERRACE, TICKETS_LOG
from elbow_helper.configuration.guild import GUILD_ID
from elbow_helper.data.events import EventsStore, record, save_state
from elbow_helper.data.reads import PeopleReads
from elbow_helper.data.store import FamilyStore
from elbow_helper.data.store.people import observe_discord, observe_messages
from elbow_helper.features.member_lifecycle import cog as cog_module
from elbow_helper.features.member_lifecycle.cog import MemberLifecycle
from elbow_helper.features.member_lifecycle.queries import LifecycleRecordSource, MemberLifecycleQueries
from elbow_helper.features.member_lifecycle.state import LifecycleState, default_state, lifecycle_event, microseconds

JOINED = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
JOINED_US = microseconds(JOINED)
OLD_JOIN = "2026-10-01T08:00:00.123456+00:00"
OLD_JOIN_US = microseconds(datetime.fromisoformat(OLD_JOIN))
SECOND_JOIN_US = microseconds(datetime(2026, 10, 2, 8, tzinfo=timezone.utc))


def saved_work():
    return {
        "members": {
            "7": {"overseer_message_id": 501, "platform": "Referral", "joined_at_iso": OLD_JOIN, "left": False},
            "8": {"overseer_message_id": 502, "platform": "Clash Champs",
                  "joined_at_iso": "2026-10-02T08:00:00+00:00", "left": False, "ticket_channel_id": 9},
            "9": {"overseer_message_id": 503, "platform": "Referral", "joined_at_iso": OLD_JOIN, "left": True},
            "10": {"overseer_message_id": 504, "platform": "free words", "joined_at_iso": OLD_JOIN, "left": False},
            "11": {"platform": "Referral", "joined_at_iso": "not a date", "left": False},
        },
        "last_seen": {"7": {"ts_iso": "2026-11-04T00:00:00+00:00", "channel_id": 70}},
        "platform_counts": {"Referral": 2, "Clash Champs": 1},
        "last_weekly_report_iso": "2026-10-30T16:00:00+00:00",
        "last_applicant_scan_iso": "2026-11-04T15:00:00+00:00",
        "overdue_applicant_ids": [8],
        "applicant_reports": {"600": {"applicant_ids": [8], "created_iso": "x", "active": True},
                              "601": {"applicant_ids": [12], "created_iso": "x", "active": False}},
        "ticket_owner_links": {"8": ["https://discord.com/channels/1/2/3"], "12": ["https://discord.com/channels/1/2/4"]},
        "ticket_log_last_message_id": "900",
        "ticket_log_index_ready": True,
    }


class FakeTextChannel:
    def __init__(self, message_id=700):
        self.send = AsyncMock(return_value=SimpleNamespace(id=message_id))


class LifecycleHistoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.path = self.root / "snapshot_intel.json"
        self.events = EventsStore(self.root / "events.sqlite3")
        self.family = FamilyStore(self.root / "family.sqlite3")
        await self.family.initialize()

    async def seed(self, data=None):
        data = saved_work() if data is None else data
        payload = {key: data.get(key, value) for key, value in default_state().items()}
        payload["applicant_reports"] = {key: value for key, value in payload["applicant_reports"].items()
                                        if value["active"]}
        def write(connection):
            save_state(connection, "lifecycle", payload)
            for member, joined in (("7", OLD_JOIN_US), ("8", SECOND_JOIN_US)):
                if member in data.get("members", {}):
                    info = data["members"][member]
                    record(connection, lifecycle_event("joined", int(member), joined,
                           {"platform": info["platform"]}, at=joined, via="import",
                           message_id=info.get("overseer_message_id")))
        await self.events.call(write)

    async def new_cog(self, people_reads=None):
        bot = SimpleNamespace(workflow_events=self.events, people_reads=people_reads)
        cog = MemberLifecycle(bot, SimpleNamespace(get_member=lambda _: None))
        cog.lifecycle_state = LifecycleState(self.events)
        await cog.lifecycle_state.startup()
        return cog

    async def new_state(self):
        state = LifecycleState(self.events)
        await state.startup()
        return state

    async def observe(self, at, rows=(), *, kind="discord_join", **kwargs):
        run = await self.family.begin_run("discord_event", "member", [(kind, str(GUILD_ID))], at)
        await self.family.requested(run, 0)
        await self.family.call(observe_discord, run, list(rows), at, scope=str(GUILD_ID), **kwargs)
        await self.family.finish(run)

    async def message(self, member_id, at, channel_id):
        run = await self.family.begin_run("discord_messages", "guild", [("discord_messages", "1")], at)
        await self.family.requested(run, 0)
        await self.family.call(observe_messages, run, [(member_id, at, channel_id)], at)
        await self.family.finish(run)

    @staticmethod
    def row(member_id, joined_us):
        return {"id": member_id, "username": "synthetic", "display_name": "Synthetic", "nick": None,
                "bot": False, "joined_at": joined_us, "roles": []}


    async def test_join_and_leave_from_discord_keep_source_reason_and_weekly_count(self):
        await self.seed({"platform_counts": {}})
        cog = await self.new_cog(PeopleReads(self.family))
        overseer = FakeTextChannel(701)

        async def audit_logs(*, limit, action):
            yield SimpleNamespace(target=SimpleNamespace(id=7), user="Leader",
                                  created_at=datetime.now(timezone.utc))

        guild = SimpleNamespace(id=GUILD_ID, get_channel=lambda _: overseer, get_role=lambda _: None,
                                audit_logs=audit_logs)
        member = SimpleNamespace(id=7, guild=guild, joined_at=JOINED, display_name="Synthetic", nick=None,
                                 mention="<@7>", roles=[], created_at=JOINED - timedelta(days=400))
        cog._find_ticket_log_links_for_member = AsyncMock(return_value=([], 0))
        with (patch.object(cog_module.discord, "TextChannel", FakeTextChannel),
              patch.object(cog_module, "snapshot_invites", AsyncMock(return_value={}))):
            await cog.on_member_join(member)
            self.assertEqual(cog.state["platform_counts"], {"Referral": 1})
            await cog.on_member_remove(member)
        embed = overseer.send.await_args_list[1].kwargs["embed"]
        self.assertIn("Kicked by Leader", [field.value for field in embed.fields])
        self.assertEqual(cog.state["platform_counts"], {})
        joined, left = await self.events.read(workflow="lifecycle")
        self.assertEqual((joined["kind"], joined["case_key"], joined["detail"], joined["source_message_id"],
                          joined["actor_kind"]), ("joined", f"7:{JOINED_US}", {"platform": "Referral"}, 701,
                                                  "automatic"))
        self.assertEqual((left["kind"], left["case_key"], left["detail"]),
                         ("left", f"7:{JOINED_US}", {"reason": "kicked"}))
        self.assertEqual(cog.lifecycle_state.open_joins(), {})
        self.assertEqual(cog.lifecycle_state.open_joins(), {})

    async def test_failed_write_keeps_state_and_history_and_a_restart_records_the_leave_once(self):
        await self.seed({"members": saved_work()["members"], "platform_counts": {"Referral": 1}})
        state = await self.new_state()
        await self.events.call(lambda c: c.execute(
            "CREATE TRIGGER fail_left BEFORE INSERT ON workflow_events WHEN NEW.kind='left' "
            "BEGIN SELECT RAISE(ABORT,'failed'); END"))
        with self.assertRaises(sqlite3.IntegrityError):
            await state.left(7, OLD_JOIN_US, "left")
        self.assertEqual(state.state["platform_counts"], {"Referral": 1})
        self.assertIn(7, state.open_joins())
        await self.events.call(lambda c: c.execute("DROP TRIGGER fail_left"))
        restarted = await self.new_state()
        self.assertEqual(restarted.state["platform_counts"], {"Referral": 1})
        await restarted.left(7, OLD_JOIN_US, "left")
        await restarted.left(7, OLD_JOIN_US, "left")
        self.assertEqual(len(await self.events.read(kind="left")), 1)
        self.assertEqual(restarted.state["platform_counts"], {})
        self.assertNotIn(7, restarted.open_joins())

    async def test_member_written_messages_never_produce_an_event(self):
        cog = await self.new_cog()
        for channel_id in (123, TICKETS_LOG, OVERSEEING_TERRACE):
            for text in ("<@7> joined the server", "Left Server: <@7> was kicked", "joined"):
                message = SimpleNamespace(
                    id=1000 + channel_id % 97, content=text, embeds=[], jump_url="https://discord.com/x",
                    guild=SimpleNamespace(id=GUILD_ID), channel=SimpleNamespace(id=channel_id),
                    author=SimpleNamespace(id=7, bot=False))
                await cog.on_message(message)
        self.assertEqual(await self.events.read(), [])
        self.assertEqual((cog.state["ticket_owner_links"], cog.lifecycle_state.open_joins()), ({}, {}))

    async def test_reads_take_stay_times_from_the_record_and_source_and_reason_from_events(self):
        state = await self.new_state()
        # The daily scan dates what the record covers.
        await self.observe(JOINED_US - 86_400_000_000, kind="discord_scan", complete=True)
        observed_join = JOINED_US + 4_000_000
        await self.observe(observed_join, [self.row(7, JOINED_US)], basis="gateway_join")
        # The event's own time is deliberately different: reads must use the record's.
        await state.joined(7, JOINED_US, "Clash Champs", 701)
        await self.message(7, JOINED_US + 60_000_000, 55)
        queries = MemberLifecycleQueries(state.snapshot, source=LifecycleRecordSource(
            state, lambda: PeopleReads(self.family)))
        current = await queries.current()
        snapshot = current.snapshot(current_members={7: "Synthetic", 8: "Other"}, activity_channels={7: 55})
        (row,) = snapshot.rows
        self.assertEqual((row.member_id, row.platform, row.joined_at, row.last_seen_channel_id),
                         (7, "Clash Champs", JOINED.isoformat(), 55))
        self.assertEqual(row.last_seen_at, (JOINED + timedelta(minutes=1)).isoformat())
        self.assertEqual(snapshot.untracked_current_member_count, 1)
        left_at = JOINED_US + 86_400_000_000
        await self.observe(left_at, departed_id=7, basis="gateway_leave")
        await state.left(7, JOINED_US, "banned", 702)
        history = await queries.history(7)
        self.assertEqual([(entry["event"], entry["at"], entry["basis"], entry["platform"], entry["reason"],
                           entry["message_id"]) for entry in history["entries"]],
                         [("join", JOINED_US, "gateway_join", "Clash Champs", None, 701),
                          ("leave", left_at, "gateway_leave", None, "banned", 702)])
        self.assertEqual(history["recorded_since"], JOINED_US - 86_400_000_000)
        # After leaving, the member is no longer a current lifecycle row.
        self.assertEqual((await queries.current()).snapshot(current_members={7: "Synthetic"},
                                                            activity_channels={}).rows, ())


    async def test_delete_member_removes_their_history_reports_and_ticket_links(self):
        await self.seed()
        state = await self.new_state()
        await self.events.delete_member(8)
        self.assertEqual({row["subject_discord_id"] for row in await self.events.read()}, {7})
        self.assertEqual(state.state["overdue_applicant_ids"], [])
        self.assertEqual(state.state["applicant_reports"]["600"]["applicant_ids"], [])
        self.assertNotIn("8", state.state["ticket_owner_links"])
        self.assertEqual(set(state.open_joins()), {7})
        await self.events.delete_member(7)
        self.assertEqual(state.open_joins(), {})
        self.assertEqual(await self.events.read(), [])


if __name__ == "__main__":
    unittest.main()
