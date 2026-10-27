from __future__ import annotations

import unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from unittest.mock import patch

import discord

from elbow_helper.configuration.roles import (
    ALLIANCE_MEMBER_ROLE_ID, CO_LEADER_ROLE_ID, HIBERNATING_ROLE_ID,
    MEMBER_ROLE_ID, SLEEPING_CO_ROLE_ID,
)
from elbow_helper.features.hibernation.commands import HibernationCommandMixin
from elbow_helper.features.hibernation.config import SNAPSHOT_ONLY_ROLE_IDS
from elbow_helper.features.hibernation.state import load_hibernation_state, save_hibernation_state
from elbow_helper.features.hibernation.tickets import HibernationTicketMixin


class HibernationPersistenceTests(unittest.TestCase):
    def test_state_write_failure_is_reported_to_the_workflow(self) -> None:
        with patch(
            "elbow_helper.features.hibernation.state.write_json_atomic",
            side_effect=OSError("disk full"),
        ):
            with (
                self.assertLogs(
                    "elbow_helper.features.hibernation.state",
                    level="ERROR",
                ),
                self.assertRaisesRegex(OSError, "disk full"),
            ):
                save_hibernation_state({"42": {"roles": [1]}})


class HibernationCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_snapshot_failure_stops_before_roles_change(self) -> None:
        owner = HibernationCommandMixin()
        member = SimpleNamespace(
            id=42,
            mention="<@42>",
            roles=[],
            remove_roles=AsyncMock(),
            add_roles=AsyncMock(),
        )
        actor = SimpleNamespace(roles=[SimpleNamespace(id=99)])
        interaction = SimpleNamespace(
            user=actor,
            guild=SimpleNamespace(get_role=lambda role_id: SimpleNamespace(id=role_id)),
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )

        with (
            patch(
                "elbow_helper.features.hibernation.commands.LEAD_PLUS",
                frozenset({99}),
            ),
            patch(
                "elbow_helper.features.hibernation.commands.load_hibernation_state",
                return_value={},
            ),
            patch(
                "elbow_helper.features.hibernation.commands.save_hibernation_member",
                side_effect=OSError("disk full"),
            ),
        ):
            with self.assertLogs(
                "elbow_helper.features.hibernation.commands",
                level="ERROR",
            ):
                await HibernationCommandMixin.hibernate_user.callback(
                    owner,
                    interaction,
                    member,
                )

        member.remove_roles.assert_not_awaited()
        member.add_roles.assert_not_awaited()
        self.assertIn(
            "couldn't move that member into hibernation",
            interaction.followup.send.await_args.args[0],
        )


class Workflow(HibernationCommandMixin, HibernationTicketMixin):
    pass


class HibernationRetryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.saved_state = {}
        self.saved_tickets = {}

        def write_state(path, data, **kwargs):
            self.saved_state = deepcopy(data)

        def write_tickets(path, data, **kwargs):
            self.saved_tickets = deepcopy(data)

        for name, replacement in (
            ("state.read_json", Mock(side_effect=lambda _: deepcopy(self.saved_state))),
            ("state.write_json_atomic", Mock(side_effect=write_state)),
            ("tickets.read_json", Mock(side_effect=lambda _: deepcopy(self.saved_tickets))),
            ("tickets.write_json_atomic", Mock(side_effect=write_tickets)),
            ("tickets.CREATED_TICKETS_FILE", Mock()),
        ):
            patcher = patch(
                f"elbow_helper.features.hibernation.{name}", replacement,
            )
            patcher.start()
            self.addCleanup(patcher.stop)
        self.reset_workflow()

    def reset_workflow(self):
        self.saved_state = {}
        self.saved_tickets = {}
        self.roles = {
            role_id: SimpleNamespace(id=role_id, name=str(role_id), is_default=lambda: False)
            for role_id in (
                MEMBER_ROLE_ID, ALLIANCE_MEMBER_ROLE_ID, CO_LEADER_ROLE_ID,
                HIBERNATING_ROLE_ID, SLEEPING_CO_ROLE_ID, SNAPSHOT_ONLY_ROLE_IDS[0],
            )
        }
        self.member = SimpleNamespace(id=42, mention="<@42>", roles=[])

        async def remove(role, **kwargs):
            self.member.roles[:] = [held for held in self.member.roles if held.id != role.id]

        async def add(role, **kwargs):
            if role not in self.member.roles:
                self.member.roles.append(role)

        self.member.remove_roles = AsyncMock(side_effect=remove)
        self.member.add_roles = AsyncMock(side_effect=add)
        self.ticket = SimpleNamespace(id=123, send=AsyncMock())
        self.guild = SimpleNamespace(
            get_role=self.roles.get,
            get_channel=lambda channel_id: self.ticket if channel_id == self.ticket.id else None,
            fetch_member=AsyncMock(return_value=self.member),
            create_text_channel=AsyncMock(return_value=self.ticket),
        )
        self.actor = SimpleNamespace(id=42)
        self.log = AsyncMock()
        self.notice = AsyncMock()
        self.achievement = AsyncMock()
        self.archive = AsyncMock()
        self.owner = self.new_owner()

    def new_owner(self):
        owner = Workflow()
        owner.send_hibernation_log = self.log
        owner._send_hibernation_notice = self.notice
        owner._archive_fallback_thread_for_member = self.archive
        owner.achievement_rewards = SimpleNamespace(track_hibernation_survivor=self.achievement)
        owner.prepare_reactivation_ticket = Mock(return_value={
            "name": "ticket", "category": None, "overwrites": {},
            "topic": self.member.mention, "welcome": "welcome", "embed": object(),
        })
        owner._build_close_ticket_view = Mock(return_value=None)
        return owner

    def fail_once(self, method, at_call=1):
        previous = method.side_effect
        attempts = 0

        async def fail(*args, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == at_call:
                raise discord.HTTPException(
                    SimpleNamespace(status=500, reason="synthetic"), "synthetic failure",
                )
            if previous is not None:
                return await previous(*args, **kwargs)
            return method.return_value

        method.side_effect = fail

    def start_hibernation(self):
        self.member.roles = [self.roles[role_id] for role_id in (
            MEMBER_ROLE_ID, ALLIANCE_MEMBER_ROLE_ID, CO_LEADER_ROLE_ID,
            SNAPSHOT_ONLY_ROLE_IDS[0],
        )]
        return self.owner.prepare_hibernation(self.guild, self.member)

    def start_reactivation(self):
        self.member.roles = [self.roles[HIBERNATING_ROLE_ID], self.roles[SLEEPING_CO_ROLE_ID]]
        save_hibernation_state({"42": {
            "roles": [MEMBER_ROLE_ID, ALLIANCE_MEMBER_ROLE_ID, CO_LEADER_ROLE_ID],
            "rank_roles": [SNAPSHOT_ONLY_ROLE_IDS[0]], "hibernation_date": "<t:123:F>",
        }})
        return self.reactivation_plan()

    def reactivation_plan(self):
        return self.owner.prepare_reactivation(
            guild=self.guild, actor=self.actor, target=self.member, force_reactivate=False,
        )

    async def test_hibernation_resumes_each_failed_step_after_restart(self):
        for stage, at_call in (("remove", 1), ("remove", 2), ("add", 2), ("log", 1), ("notice", 1)):
            with self.subTest(stage=stage, at_call=at_call):
                self.reset_workflow()
                plan = self.start_hibernation()
                expected_roles = plan["stored_role_ids"]
                failing = {"remove": self.member.remove_roles, "add": self.member.add_roles,
                           "log": self.log, "notice": self.notice}[stage]
                self.fail_once(failing, at_call)
                reply = AsyncMock()
                with self.assertRaises(discord.HTTPException):
                    await self.owner.hibernate_member(self.guild, self.actor, plan, reply=reply)
                reply.assert_not_awaited()
                saved = load_hibernation_state()["42"]
                self.assertEqual(saved["roles"], expected_roles)
                pending = saved["hibernation_progress"]
                removals = self.member.remove_roles.await_count
                additions = self.member.add_roles.await_count
                remaining_remove = len(pending["to_remove"])
                remaining_add = len(pending["to_add"])
                self.owner = self.new_owner()
                retry = self.owner.prepare_hibernation(self.guild, self.member)
                self.assertIsNone(retry["issue"])
                self.assertEqual(retry["stored_role_ids"], expected_roles)
                message = await self.owner.hibernate_member(self.guild, self.actor, retry, reply=reply)
                self.assertEqual(message, "Moved <@42> to hibernation.")
                reply.assert_awaited_once_with(message)
                self.assertEqual(self.member.remove_roles.await_count, removals + remaining_remove)
                self.assertEqual(self.member.add_roles.await_count, additions + remaining_add)
                self.assertEqual(load_hibernation_state()["42"]["roles"], expected_roles)
                self.assertNotIn("hibernation_progress", load_hibernation_state()["42"])
                self.assertEqual(self.log.await_count, 2 if stage == "log" else 1)
                self.assertEqual(self.owner.prepare_hibernation(self.guild, self.member)["issue"],
                                 "That member is already hibernating.")

    async def test_reactivation_resumes_each_failed_step_and_retains_saved_roles(self):
        for stage, at_call in (
            ("remove", 2), ("add", 2), ("achievement", 1), ("fetch", 1),
            ("create", 1), ("send", 1), ("send", 2), ("archive", 1),
        ):
            with self.subTest(stage=stage, at_call=at_call):
                self.reset_workflow()
                plan = self.start_reactivation()
                expected_roles = plan["info"]["roles"]
                failing = {"remove": self.member.remove_roles, "add": self.member.add_roles,
                           "achievement": self.achievement, "fetch": self.guild.fetch_member,
                           "create": self.guild.create_text_channel, "send": self.ticket.send,
                           "archive": self.archive}[stage]
                self.fail_once(failing, at_call)
                with self.assertRaises(discord.HTTPException):
                    await self.owner.reactivate_member(plan)
                saved = load_hibernation_state()["42"]
                self.assertEqual(saved["roles"], expected_roles)
                pending = saved["reactivation_progress"]
                removals = self.member.remove_roles.await_count
                additions = self.member.add_roles.await_count
                remaining_remove = len(pending["to_remove"])
                remaining_add = len(pending["to_add"])
                self.owner = self.new_owner()
                result = await self.owner.reactivate_member(self.reactivation_plan())
                self.assertEqual(result["message"], "Welcome back. Your roles have been restored.")
                self.assertEqual(result["ticket_channel_id"], 123)
                self.assertNotIn("42", load_hibernation_state())
                self.assertEqual(self.member.remove_roles.await_count, removals + remaining_remove)
                self.assertEqual(self.member.add_roles.await_count, additions + remaining_add)
                self.assertEqual(self.guild.create_text_channel.await_count, 2 if stage == "create" else 1)
                self.assertEqual(self.ticket.send.await_count, 3 if stage == "send" else 2)
                self.assertEqual(self.achievement.await_count, 2 if stage == "achievement" else 1)
                self.assertEqual({role.id for role in self.member.roles}, set(expected_roles))

    async def test_registry_write_failure_reuses_ticket_and_keeps_snapshot(self):
        plan = self.start_reactivation()
        with patch("elbow_helper.features.hibernation.tickets.write_json_atomic",
                   side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                await self.owner.reactivate_member(plan)
        self.assertEqual(load_hibernation_state()["42"]["roles"], plan["info"]["roles"])
        self.owner = self.new_owner()
        await self.owner.reactivate_member(self.reactivation_plan())
        self.guild.create_text_channel.assert_awaited_once()
        self.assertEqual(self.ticket.send.await_count, 2)

    async def test_notice_failure_is_not_silently_treated_as_delivered(self):
        self.member.send = AsyncMock()
        self.fail_once(self.member.send)
        self.owner._build_reactivate_view = Mock(return_value=None)
        self.owner._post_hibernation_notice_in_fallback_thread = AsyncMock(return_value=False)
        with self.assertRaisesRegex(RuntimeError, "could not be delivered"):
            await HibernationTicketMixin._send_hibernation_notice(self.owner, self.member)

    async def test_archive_failure_is_not_silently_treated_as_complete(self):
        save_hibernation_state({"_fallback_threads": {"42": {"thread_id": 456}}})
        thread = SimpleNamespace(archived=False, edit=AsyncMock())
        self.owner.bot = SimpleNamespace(get_channel=lambda _: thread)
        self.fail_once(thread.edit)
        with self.assertLogs("elbow_helper.features.hibernation.tickets", level="WARNING"):
            with self.assertRaises(discord.HTTPException):
                await HibernationTicketMixin._archive_fallback_thread_for_member(
                    self.owner, 42, reason="synthetic",
                )

    async def test_step_writes_preserve_new_fallback_metadata(self):
        plan = self.start_hibernation()

        async def notice(member):
            data = load_hibernation_state()
            data["_fallback_threads"] = {"42": {"thread_id": 456}}
            save_hibernation_state(data)

        self.notice.side_effect = notice
        await self.owner.hibernate_member(self.guild, self.actor, plan)
        self.assertEqual(load_hibernation_state()["_fallback_threads"], {"42": {"thread_id": 456}})


if __name__ == "__main__":
    unittest.main()
