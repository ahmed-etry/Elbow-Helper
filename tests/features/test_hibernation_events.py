import asyncio
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from elbow_helper.configuration.roles import ALLIANCE_MEMBER_ROLE_ID, HIBERNATING_ROLE_ID, MEMBER_ROLE_ID
from elbow_helper.data.events import EventsStore, load_state, record
from elbow_helper.features.hibernation.commands import HibernationCommandMixin
from elbow_helper.features.hibernation.queries import HibernationQueries
from elbow_helper.features.hibernation.state import HibernationState, HibernationStateReader, hibernation_event
from elbow_helper.features.hibernation.tickets import HibernationTicketMixin
from elbow_helper.features.support_tickets.state import TicketState


class Workflow(HibernationCommandMixin, HibernationTicketMixin):
    pass


class HibernationHistoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.db = self.root / "events.sqlite3"
        self.path = self.root / "hibernation.json"
        self.roles = {i: SimpleNamespace(id=i, name=str(i), is_default=lambda: False)
                      for i in (MEMBER_ROLE_ID, ALLIANCE_MEMBER_ROLE_ID, HIBERNATING_ROLE_ID)}
        self.member = SimpleNamespace(id=42, mention="<@42>", roles=[
            self.roles[MEMBER_ROLE_ID], self.roles[ALLIANCE_MEMBER_ROLE_ID]])

        async def remove(role, **kwargs):
            self.member.roles[:] = [held for held in self.member.roles if held.id != role.id]

        async def add(role, **kwargs):
            self.member.roles.append(role)

        self.member.remove_roles = AsyncMock(side_effect=remove)
        self.member.add_roles = AsyncMock(side_effect=add)
        self.actor = SimpleNamespace(id=7)
        self.ticket = SimpleNamespace(id=123, send=AsyncMock())
        self.guild = SimpleNamespace(
            get_role=self.roles.get, fetch_member=AsyncMock(return_value=self.member),
            get_channel=lambda i: self.ticket if i == 123 else None,
            create_text_channel=AsyncMock(return_value=self.ticket),
        )
        self.plan = {"user": self.member, "stored_role_ids": [MEMBER_ROLE_ID, ALLIANCE_MEMBER_ROLE_ID],
                     "snapshot_role_ids": [], "unix_ts": 1700000000,
                     "to_remove": tuple(self.member.roles), "to_add": (self.roles[HIBERNATING_ROLE_ID],)}
        self.owner = await self.new_owner()

    async def new_owner(self):
        events = EventsStore(self.db)
        state = HibernationState(events, self.path)
        await state.startup()
        tickets = TicketState(events, self.root / "tickets.json")
        await tickets.startup()
        owner = Workflow()
        owner.hibernation_state = state
        owner.bot = SimpleNamespace(get_cog=lambda _: SimpleNamespace(ticket_state=tickets))
        owner.send_hibernation_log = AsyncMock()
        owner._send_hibernation_notice = AsyncMock()
        owner._archive_fallback_thread_for_member = AsyncMock()
        owner.achievement_rewards = SimpleNamespace(track_hibernation_survivor=AsyncMock())
        owner.prepare_reactivation_ticket = Mock(return_value={
            "name": "ticket", "category": None, "overwrites": {}, "topic": "<@42>",
            "welcome": "welcome", "embed": None,
        })
        owner._build_close_ticket_view = Mock(return_value=None)
        return owner

    def reactivation(self, *, forced=False):
        return self.owner.prepare_reactivation(guild=self.guild, actor=self.member, target=self.member,
                                               force_reactivate=forced)

    async def test_import_only_open_members_once_and_preserve_all_markers(self):
        legacy = {"42": {"roles": [1, 2], "rank_roles": [3], "hibernation_date": "<t:1700000000:F>"},
                  "43": {"roles": [4], "hibernation_date": "<t:1700000001:F>",
                         "hibernation_progress": {"to_remove": [4], "to_add": [5], "unix_ts": 1700000001}},
                  "_fallback_info_message_id": 456,
                  "_fallback_threads": {"42": {"user_id": 42, "thread_id": 789}}}
        other_path = self.root / "legacy.json"
        other_path.write_text(json.dumps(legacy))
        events = EventsStore(self.root / "import.sqlite3")
        state = HibernationState(events, other_path)
        await state.startup()
        snapshot = state.snapshot()
        for key in legacy:
            actual = deepcopy(snapshot[key])
            if key.isdecimal():
                actual.pop("_case_started_at")
            self.assertEqual(actual, legacy[key])
        rows = await events.read()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["subject_discord_id"], 42)
        self.assertEqual(rows[0]["actor_kind"], "import")
        self.assertEqual(rows[0]["detail"], {"roles_taken": [1, 2]})
        restarted = HibernationState(EventsStore(events.path), other_path)
        await restarted.startup()
        self.assertEqual(restarted.state, snapshot)
        self.assertEqual(await restarted.events.read(), rows)

    async def test_crash_mid_hibernation_resumes_from_persisted_role_checkpoint(self):
        original = self.member.remove_roles.side_effect
        async def fail_second(role, **kwargs):
            if role.id == ALLIANCE_MEMBER_ROLE_ID:
                raise RuntimeError("crash mid-hibernation")
            await original(role, **kwargs)
        self.member.remove_roles.side_effect = fail_second
        with self.assertRaisesRegex(RuntimeError, "crash"):
            await self.owner.hibernate_member(self.guild, self.actor, self.plan)
        before = self.owner.hibernation_member_state(42)
        self.assertEqual(before["hibernation_progress"]["to_remove"], [ALLIANCE_MEMBER_ROLE_ID])
        self.assertEqual(await self.owner.hibernation_state.events.read(), [])
        self.owner = await self.new_owner()
        self.assertEqual(self.owner.hibernation_member_state(42), before)
        self.member.remove_roles.side_effect = original
        plan = self.owner.prepare_hibernation(self.guild, self.member)
        await self.owner.hibernate_member(self.guild, SimpleNamespace(id=8), plan)
        calls = [args.args[0].id for args in self.member.remove_roles.await_args_list]
        self.assertEqual(calls, [MEMBER_ROLE_ID, ALLIANCE_MEMBER_ROLE_ID, ALLIANCE_MEMBER_ROLE_ID])
        rows = await self.owner.hibernation_state.events.read()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["actor_discord_id"], 7)
        self.assertEqual(rows[0]["detail"]["roles_taken"], self.plan["stored_role_ids"])

    async def test_failed_role_checkpoint_reconciles_applied_role_after_restart(self):
        state = self.owner.hibernation_state
        save = state.save_member
        count = 0
        async def fail_checkpoint(*args, **kwargs):
            nonlocal count
            count += 1
            if count == 2:
                raise sqlite3.OperationalError("write failed after role removal")
            await save(*args, **kwargs)
        with patch.object(state, "save_member", side_effect=fail_checkpoint):
            with self.assertRaises(sqlite3.OperationalError):
                await self.owner.hibernate_member(self.guild, self.actor, self.plan)
        self.owner = await self.new_owner()
        plan = self.owner.prepare_hibernation(self.guild, self.member)
        await self.owner.hibernate_member(self.guild, self.actor, plan)
        self.assertEqual([call.args[0].id for call in self.member.remove_roles.await_args_list],
                         [MEMBER_ROLE_ID, ALLIANCE_MEMBER_ROLE_ID])

    async def test_failed_final_event_keeps_checkpoints_and_does_not_resend_notice(self):
        events = self.owner.hibernation_state.events
        await events.call(lambda c: c.execute(
            "CREATE TRIGGER fail_event BEFORE INSERT ON workflow_events BEGIN SELECT RAISE(ABORT,'failed'); END"))
        with self.assertRaises(sqlite3.IntegrityError):
            await self.owner.hibernate_member(self.guild, self.actor, self.plan)
        pending = self.owner.hibernation_member_state(42)["hibernation_progress"]
        self.assertTrue(pending["notice_sent"])
        self.assertEqual(await events.read(), [])
        await events.call(lambda c: c.execute("DROP TRIGGER fail_event"))
        self.owner = await self.new_owner()
        await self.owner.hibernate_member(self.guild, self.actor,
                                          self.owner.prepare_hibernation(self.guild, self.member))
        self.owner._send_hibernation_notice.assert_not_awaited()
        self.owner.send_hibernation_log.assert_not_awaited()
        self.assertEqual(len(await events.read()), 1)

    async def test_reactivation_event_and_removal_are_atomic_and_ticket_is_reused(self):
        await self.owner.hibernate_member(self.guild, self.actor, self.plan)
        events = self.owner.hibernation_state.events
        await events.call(lambda c: c.execute(
            "CREATE TRIGGER fail_reactivate BEFORE INSERT ON workflow_events WHEN NEW.kind='reactivated' "
            "BEGIN SELECT RAISE(ABORT,'failed'); END"))
        with self.assertRaises(sqlite3.IntegrityError):
            await self.owner.reactivate_member(self.reactivation())
        pending = self.owner.hibernation_member_state(42)
        self.assertEqual(pending["roles"], self.plan["stored_role_ids"])
        self.assertTrue(pending["reactivation_progress"]["controls_sent"])
        await events.call(lambda c: c.execute("DROP TRIGGER fail_reactivate"))
        self.owner = await self.new_owner()
        await self.owner.reactivate_member(self.reactivation())
        self.guild.create_text_channel.assert_awaited_once()
        self.assertEqual(self.ticket.send.await_count, 2)
        self.assertIsNone(self.owner.hibernation_member_state(42))
        rows = await events.read(workflow="hibernation")
        self.assertEqual([row["kind"] for row in rows], ["hibernated", "reactivated"])
        self.assertEqual(rows[1]["detail"], {"forced": False, "ticket_channel_id": 123})
        support = await events.read(workflow="support")
        self.assertEqual(len(support), 1)
        self.assertEqual(support[0]["subject_discord_id"], 42)
        self.assertEqual(support[0]["detail"], {"source": "reactivation"})

    async def test_fallback_updates_and_member_checkpoint_preserve_each_other(self):
        state = self.owner.hibernation_state
        info = {"roles": [1], "hibernation_date": "<t:1700000000:F>", "_case_started_at": 1700000000000000}
        await asyncio.gather(state.save_member(42, info), state.save_fallback_info(123),
                             state.save_fallback_thread(42, {"thread_id": 456}))
        snapshot = state.snapshot()
        self.assertEqual(snapshot["42"], info)
        self.assertEqual(snapshot["_fallback_info_message_id"], 123)
        self.assertEqual(snapshot["_fallback_threads"], {"42": {"thread_id": 456}})
        reader = HibernationStateReader(state)
        reader.get_member(42)["roles"].append(2)
        self.assertEqual(reader.get_member(42)["roles"], [1])
        self.assertTrue(HibernationQueries(state.snapshot).is_hibernating(42))
        await state.save_fallback_thread(42, None)
        self.assertEqual(state.snapshot()["_fallback_threads"], {})

    async def test_mirror_failure_recovers_from_sqlite_on_restart(self):
        with patch("elbow_helper.features.hibernation.state.write_json_atomic", side_effect=OSError("full")):
            with self.assertLogs("elbow_helper.features.hibernation.state", level="ERROR"):
                await self.owner.hibernate_member(self.guild, self.actor, self.plan)
        before = self.owner.hibernation_state.snapshot()
        self.owner = await self.new_owner()
        self.assertEqual(self.owner.hibernation_state.snapshot(), before)
        self.assertEqual(json.loads(self.path.read_text()), before)
        self.assertEqual(len(await self.owner.hibernation_state.events.read()), 1)

    async def test_concurrent_hibernations_do_not_duplicate_events_or_notices(self):
        await asyncio.gather(*(self.owner.hibernate_member(self.guild, self.actor, self.plan) for _ in range(2)))
        self.owner._send_hibernation_notice.assert_awaited_once()
        self.assertEqual(len(await self.owner.hibernation_state.events.read()), 1)

    async def test_delete_member_removes_entry_threads_history_and_mirror(self):
        await self.owner.hibernate_member(self.guild, self.actor, self.plan)
        state = self.owner.hibernation_state
        await state.save_fallback_thread(42, {"thread_id": 456})
        await state.events.delete_member(42)
        self.assertNotIn("42", state.snapshot())
        self.assertEqual(state.snapshot()["_fallback_threads"], {})
        self.assertEqual(await state.events.read(), [])
        self.assertEqual(json.loads(self.path.read_text()), state.snapshot())

    async def test_bad_legacy_data_does_not_mark_import_complete(self):
        for payload in ('{', '[]'):
            self.path.write_text(payload)
            state = HibernationState(EventsStore(self.root / ("bad" + str(len(payload)) + ".sqlite3")), self.path)
            with self.assertRaises(ValueError):
                await state.startup()
            self.assertIsNone(await state.events.call(load_state, "hibernation"))

    async def test_role_and_reactivation_details_reject_free_text(self):
        info = {"roles": [1], "_case_started_at": 1700000000000000}
        events = self.owner.hibernation_state.events
        for detail in ({"roles_taken": ["words"]}, {"roles_taken": [True]},
                       {"roles_taken": [1], "reason": "words"}):
            event = hibernation_event(42, info, "hibernated", actor_id=7)
            with self.assertRaises(ValueError):
                await events.call(record, replace(event, detail=detail))
        event = hibernation_event(42, info, "reactivated", actor_id=7, ticket_id=123)
        with self.assertRaises(ValueError):
            await events.call(record, replace(event, detail={"forced": "yes", "ticket_channel_id": 123}))

    async def test_deleting_actor_scrubs_pending_attribution_before_completion(self):
        self.owner._send_hibernation_notice.side_effect = RuntimeError("not delivered")
        with self.assertRaises(RuntimeError):
            await self.owner.hibernate_member(self.guild, self.actor, self.plan)
        await self.owner.hibernation_state.events.delete_member(7)
        self.assertIsNone(self.owner.hibernation_member_state(42)["hibernation_progress"]["actor_discord_id"])
        self.owner = await self.new_owner()
        await self.owner.hibernate_member(self.guild, SimpleNamespace(id=8),
                                          self.owner.prepare_hibernation(self.guild, self.member))
        row, = await self.owner.hibernation_state.events.read()
        self.assertIsNone(row["actor_discord_id"])
        self.assertEqual(row["actor_kind"], "automatic")

    async def test_cancellation_waits_for_checkpoint_and_publishes_committed_memory(self):
        state = self.owner.hibernation_state
        original = state.events.call
        started, release = asyncio.Event(), asyncio.Event()
        async def slow(*args, **kwargs):
            started.set()
            await release.wait()
            return await original(*args, **kwargs)
        with patch.object(state.events, "call", side_effect=slow):
            task = asyncio.create_task(state.save_fallback_info(123))
            await started.wait()
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(state.snapshot()["_fallback_info_message_id"], 123)
        restarted = HibernationState(EventsStore(self.db), self.path)
        await restarted.startup()
        self.assertEqual(restarted.snapshot(), state.snapshot())

    async def test_forced_agent_reactivation_keeps_actor_and_mode(self):
        await self.owner.hibernate_member(self.guild, self.actor, self.plan)
        plan = self.reactivation()
        plan.update(actor=self.actor, force_reactivate=True, via="agent")
        await self.owner.reactivate_member(plan)
        row, = await self.owner.hibernation_state.events.read(kind="reactivated")
        self.assertTrue(row["detail"]["forced"])
        self.assertEqual(row["actor_discord_id"], 7)
        self.assertEqual(row["actor_kind"], "agent")
        self.assertEqual(row["via"], "agent")
