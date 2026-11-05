import asyncio
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.data.events import EventsStore, load_state, record
from elbow_helper.features.support_tickets.state import TicketState, ticket_event
from elbow_helper.features.support_tickets.commands import SupportCommandMixin
from elbow_helper.features.support_tickets.views import SupportTicketConfirmView


class TicketHistoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "tickets.json"
        self.db = Path(self.directory.name) / "events.sqlite3"
        self.events = EventsStore(self.db)
        self.state = TicketState(self.events, self.path)
        await self.state.startup()
        self.info = {"owner": 42, "topic": "Private words", "source": "open",
                     "created_at": "2026-11-04T00:00:00+00:00"}

    async def test_cycles_are_kept_and_retries_are_deduplicated(self):
        self.assertTrue(await self.state.register(123, self.info, actor_id=7, via="agent"))
        self.assertFalse(await self.state.register(123, self.info, actor_id=7, via="agent"))
        for _ in range(2):
            self.assertTrue(await self.state.transition(123, "closed", actor_id=7))
            self.assertFalse(await self.state.transition(123, "closed", actor_id=7))
            self.assertTrue(await self.state.transition(123, "open", actor_id=42, via="button"))
        await self.state.transition(123, "deleted", actor_id=7)
        self.assertFalse(await self.state.transition(123, "deleted", via="schedule"))
        rows = await self.events.read(workflow="support", case="123", subject=42)
        self.assertEqual([row["kind"] for row in rows], ["ticket_opened", "ticket_closed",
                         "ticket_reopened", "ticket_closed", "ticket_reopened", "ticket_deleted"])
        self.assertEqual(rows[0]["actor_kind"], "agent")
        self.assertTrue(all(row["detail"] == {"source": "open"} for row in rows))
        self.assertIsNone(self.state.registration(123))
        self.assertEqual(json.loads(self.path.read_text()), {})

    async def test_import_once_and_preserve_unknown_reactivation_owner(self):
        self.path.write_text(json.dumps({"123": self.info, "456": {
            "created_by": "7", "created_at": self.info["created_at"], "source": "reactivation"}}))
        self.events = EventsStore(self.db.parent / "import.sqlite3")
        first = TicketState(self.events, self.path)
        await first.startup()
        rows = await self.events.read()
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row["actor_kind"] == "import" for row in rows))
        self.assertIsNone(rows[1]["subject_discord_id"])
        second = TicketState(EventsStore(self.events.path), self.path)
        await second.startup()
        self.assertEqual(first.state, second.state)
        self.assertEqual(len(await second.events.read()), 2)

    async def test_failed_event_rolls_back_registration_and_memory(self):
        await self.events.call(lambda c: c.execute(
            "CREATE TRIGGER fail_event BEFORE INSERT ON workflow_events BEGIN SELECT RAISE(ABORT,'failed'); END"))
        with self.assertRaises(sqlite3.IntegrityError):
            await self.state.register(123, self.info, actor_id=7)
        self.assertEqual(self.state.state, {})
        self.assertEqual(await self.events.call(load_state, "support"), {})
        self.assertEqual(await self.events.read(), [])

    async def test_failed_mirror_recovers_on_restart_without_reimport(self):
        with patch("elbow_helper.features.support_tickets.state.write_json_atomic", side_effect=OSError("full")):
            with self.assertLogs("elbow_helper.features.support_tickets.state", level="ERROR"):
                await self.state.register(123, self.info, actor_id=7)
        restarted = TicketState(EventsStore(self.db), self.path)
        await restarted.startup()
        self.assertEqual(restarted.registration(123), self.info)
        self.assertEqual(len(await restarted.events.read()), 1)
        self.assertEqual(json.loads(self.path.read_text())["123"], self.info)

    async def test_failed_import_is_retryable_and_corruption_is_not_an_empty_import(self):
        self.path.write_text('{')
        fresh = TicketState(EventsStore(self.db.parent / "bad.sqlite3"), self.path)
        with self.assertRaises(ValueError):
            await fresh.startup()
        self.assertIsNone(await fresh.events.call(load_state, "support"))
        self.path.write_text(json.dumps({"123": self.info}))
        await fresh.startup()
        self.assertEqual(len(await fresh.events.read()), 1)

    async def test_concurrent_registrations_do_not_overwrite_each_other(self):
        await asyncio.gather(*(self.state.register(i, self.info, actor_id=7) for i in range(100, 110)))
        self.assertEqual(len(self.state.state), 10)
        self.assertEqual(len(await self.events.read()), 10)

    async def test_member_deletion_refreshes_state_and_mirror(self):
        await self.state.register(123, self.info, actor_id=7)
        await self.events.delete_member(42)
        self.assertIsNone(self.state.registration(123))
        self.assertEqual(json.loads(self.path.read_text()), {})

    async def test_free_text_cannot_enter_events(self):
        event = ticket_event(123, self.info, "ticket_opened", actor_id=7)
        for detail in ({"source": "member words"}, {"source": "open", "topic": "words"}):
            with self.assertRaises(ValueError):
                await self.events.call(record, replace(event, detail=detail))

    async def test_reopen_permission_failure_keeps_history_open(self):
        import discord
        await self.state.register(123, self.info, actor_id=7)
        await self.state.transition(123, "closed", actor_id=7)
        owner = SimpleNamespace(id=42)
        workflow = SupportCommandMixin()
        workflow.ticket_state = self.state
        channel = SimpleNamespace(id=123, topic=None, set_permissions=AsyncMock(
            side_effect=discord.Forbidden(SimpleNamespace(status=403, reason="denied"), "denied")))
        with self.assertLogs("elbow_helper.features.support_tickets.commands", level="WARNING"):
            _, restored = await workflow.reopen_support_ticket(SimpleNamespace(get_member=lambda _: owner),
                                                               channel, SimpleNamespace(id=7))
        self.assertFalse(restored)
        self.assertEqual(len(await self.events.read()), 2)

    async def test_failed_channel_delete_keeps_registration(self):
        await self.state.register(123, self.info, actor_id=7)
        channel = SimpleNamespace(id=123, delete=AsyncMock(side_effect=RuntimeError("failed")))
        interaction = SimpleNamespace(channel=channel, user=SimpleNamespace(id=7, roles=[SimpleNamespace(id=8)]),
                                      client=None, response=SimpleNamespace(send_message=AsyncMock()))
        view = SupportTicketConfirmView(SimpleNamespace(ticket_state=self.state))
        with patch("elbow_helper.features.support_tickets.views.LEAD", {8}), \
                patch("elbow_helper.features.support_tickets.views.loading_status", AsyncMock(return_value="pending")):
            with self.assertRaises(RuntimeError):
                await view.delete_ticket.callback(interaction)
        self.assertIsNotNone(self.state.registration(123))
        self.assertEqual(len(await self.events.read()), 1)

    async def test_gateway_deletion_during_button_delete_keeps_actor_and_one_event(self):
        await self.state.register(123, self.info, actor_id=7)
        channel = SimpleNamespace(id=123, delete=AsyncMock())
        # A real gateway callback can run before the HTTP delete returns.
        async def delete():
            await self.state.channel_deleted(123)
        channel.delete.side_effect = delete
        await self.state.delete_channel(channel, actor_id=7)
        rows = await self.events.read(kind="ticket_deleted")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["actor_discord_id"], 7)
        self.assertEqual(rows[0]["via"], "button")
        self.assertEqual(self.state._deleting, {})

    async def test_failed_transition_write_keeps_previous_status_and_event_history(self):
        await self.state.register(123, self.info, actor_id=7)
        await self.events.call(lambda c: c.execute(
            "CREATE TRIGGER fail_event BEFORE INSERT ON workflow_events BEGIN SELECT RAISE(ABORT,'failed'); END"))
        for status in ("closed", "deleted"):
            with self.assertRaises(sqlite3.IntegrityError):
                await self.state.transition(123, status, actor_id=7)
            self.assertEqual(self.state.registration(123), self.info)
        self.assertEqual(len(await self.events.read()), 1)

    async def test_external_delete_has_automatic_origin(self):
        await self.state.register(123, self.info, actor_id=7)
        await self.state.channel_deleted(123)
        row, = await self.events.read(kind="ticket_deleted")
        self.assertEqual(row["actor_kind"], "automatic")
        self.assertEqual(row["via"], "schedule")
