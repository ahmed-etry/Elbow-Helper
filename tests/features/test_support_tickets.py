from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from elbow_helper.data.events.testing import MemoryEventsStore
from elbow_helper.features.hibernation.tickets import HibernationTicketMixin
from elbow_helper.features.support_tickets.commands import SupportCommandMixin
from elbow_helper.features.support_tickets.state import TicketState


class _FakeTextChannel:
    def __init__(self, transcript_status_message: object, *, fail_controls: bool) -> None:
        self.id = 123
        self.name = "support-member"
        self.topic = None
        self._transcript_status_message = transcript_status_message
        self._fail_controls = fail_controls
        self._send_count = 0

    async def send(self, **kwargs):
        self._send_count += 1
        if self._send_count == 1:
            return SimpleNamespace()
        if self._send_count == 2:
            return self._transcript_status_message
        if self._fail_controls:
            raise RuntimeError("controls unavailable")
        return SimpleNamespace()

    def history(self, **kwargs):
        async def empty_history():
            if False:
                yield None

        return empty_history()


class SupportTicketCloseTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_transcripts_keep_both_ticket_types_open_until_another_close(self):
        for reactivation in (False, True):
            with self.subTest(reactivation=reactivation):
                events = MemoryEventsStore()
                self.addCleanup(events.close)
                state = TicketState(events)
                await state.startup()
                await state.register(123, {"owner": 42, "source": "reactivation" if reactivation else "open"}, actor_id=7)
                member = SimpleNamespace(id=42, name="Member", display_name="Member",
                                         display_avatar=SimpleNamespace(url="https://example.com/avatar.png"))
                channel = MagicMock(spec=discord.TextChannel)
                channel.id, channel.name, channel.topic = 123, "ticket-member", "<@42>"
                channel.send = AsyncMock(return_value=SimpleNamespace(edit=AsyncMock()))
                channel.set_permissions = AsyncMock()

                async def history(**kwargs):
                    if False:
                        yield None
                channel.history = history
                log = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(id=456)))
                guild = SimpleNamespace(filesize_limit=8 * 1024 * 1024,
                                        get_channel=lambda _: log, get_member=lambda _: member)
                actor = SimpleNamespace(id=7, mention="<@7>")
                workflow = HibernationTicketMixin() if reactivation else SupportCommandMixin()
                workflow.ticket_state = state
                workflow.bot = SimpleNamespace(get_cog=lambda _: workflow)
                workflow._build_close_ticket_confirm_view = MagicMock(return_value=None)
                workflow.build_confirm_view = MagicMock(return_value=None)
                workflow._build_transcript_link_view = MagicMock(return_value=None)
                report = AsyncMock()
                module = "elbow_helper.features.hibernation.tickets" if reactivation else "elbow_helper.features.support_tickets.commands"

                async def close():
                    if reactivation:
                        return await workflow.close_reactivation_ticket(guild, channel, actor, report=report)
                    return await workflow.close_support_ticket(workflow.prepare_support_close(guild, channel, actor), report)

                with patch(module + ".chat_exporter.export", AsyncMock(return_value=None)):
                    result = await close()
                self.assertFalse(result[0] if reactivation else result["status"] == "complete")
                self.assertEqual(state.registration(123).get("_status", "open"), "open")
                self.assertEqual(await events.read(kind="ticket_closed"), [])
                log.send.assert_not_awaited()
                with patch(module + ".chat_exporter.export", AsyncMock(return_value="<html>transcript</html>")):
                    result = await close()
                self.assertTrue(result[0] if reactivation else result["status"] == "complete")
                self.assertEqual(state.registration(123)["_status"], "closed")
                self.assertEqual(len(await events.read(kind="ticket_closed")), 1)
                log.send.assert_awaited_once()

    @staticmethod
    def _interaction(channel: _FakeTextChannel, guild: object) -> SimpleNamespace:
        return SimpleNamespace(
            guild=guild,
            channel=channel,
            user=SimpleNamespace(
                roles=[SimpleNamespace(id=1)],
                id=7,
                mention="<@7>",
            ),
            response=SimpleNamespace(
                is_done=MagicMock(return_value=False),
                defer=AsyncMock(),
            ),
            followup=SimpleNamespace(send=AsyncMock()),
        )

    async def _run_close(
        self,
        *,
        transcript_status_message: object,
        fail_controls: bool,
    ) -> tuple[SimpleNamespace, AsyncMock]:
        channel = _FakeTextChannel(
            transcript_status_message,
            fail_controls=fail_controls,
        )
        log_send = AsyncMock(return_value=SimpleNamespace())
        guild = SimpleNamespace(
            filesize_limit=8 * 1024 * 1024,
            get_channel=MagicMock(return_value=SimpleNamespace(send=log_send)),
            get_member=MagicMock(return_value=None),
        )
        interaction = self._interaction(channel, guild)
        commands = SupportCommandMixin()
        commands.ticket_state = SimpleNamespace(
            registration=MagicMock(return_value={"source": "open"}),
            register=AsyncMock(side_effect=OSError("disk full")), transition=AsyncMock(),
        )
        commands.build_confirm_view = MagicMock(return_value=SimpleNamespace())

        with (
            patch("elbow_helper.features.support_tickets.commands.LEAD", {1}),
            patch("elbow_helper.features.support_tickets.commands.RECRUITERS", set()),
            patch("elbow_helper.features.support_tickets.commands.SUPPORT_TRANSCRIPTS", 456),
            patch("elbow_helper.features.support_tickets.commands.discord.TextChannel", _FakeTextChannel),
            patch(
                "elbow_helper.features.support_tickets.commands.chat_exporter.export",
                new=AsyncMock(return_value="<html>transcript</html>"),
            ),
        ):
            await commands._handle_close_ticket(interaction)

        return interaction, log_send

    async def test_controls_failure_does_not_replace_saved_transcript_status(self) -> None:
        status_edit = AsyncMock()
        status_message = SimpleNamespace(edit=status_edit)

        with self.assertLogs("elbow_helper.features.support_tickets.commands", level="ERROR"):
            interaction, log_send = await self._run_close(
                transcript_status_message=status_message,
                fail_controls=True,
            )

        log_send.assert_awaited_once()
        status_edit.assert_awaited_once()
        self.assertEqual(
            status_edit.call_args.kwargs["embed"].description,
            "Transcript saved to <#456>",
        )
        interaction.followup.send.assert_not_awaited()

    async def test_status_update_failure_reports_that_transcript_was_saved(self) -> None:
        status_edit = AsyncMock(side_effect=RuntimeError("status unavailable"))
        status_message = SimpleNamespace(edit=status_edit)

        with self.assertLogs("elbow_helper.features.support_tickets.commands", level="ERROR"):
            interaction, log_send = await self._run_close(
                transcript_status_message=status_message,
                fail_controls=False,
            )

        log_send.assert_awaited_once()
        status_edit.assert_awaited_once()
        interaction.followup.send.assert_awaited_once_with(
            "Transcript saved to <#456>",
            ephemeral=True,
        )


class SupportTicketCreationTests(unittest.IsolatedAsyncioTestCase):

    async def test_state_failure_removes_the_untracked_channel(self) -> None:
        ticket_channel = SimpleNamespace(
            id=123,
            mention="<#123>",
            edit=AsyncMock(),
            send=AsyncMock(),
            delete=AsyncMock(),
        )
        guild = SimpleNamespace(
            default_role=MagicMock(),
            me=None,
            get_member=MagicMock(return_value=None),
            get_role=MagicMock(return_value=None),
            get_channel=MagicMock(return_value=SimpleNamespace()),
            create_text_channel=AsyncMock(return_value=ticket_channel),
        )
        interaction = SimpleNamespace(
            user=SimpleNamespace(
                id=7, roles=[SimpleNamespace(id=1)],
                display_name="Lead",
                display_avatar=SimpleNamespace(url="https://example.com/avatar.png"),
            ),
            guild=guild,
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        member = MagicMock()
        member.id = 42
        member.mention = "<@42>"
        member.display_name = "Member"
        member.name = "member"
        commands = SupportCommandMixin()
        commands.ticket_state = SimpleNamespace(
            registration=MagicMock(return_value={"source": "open"}),
            register=AsyncMock(side_effect=OSError("disk full")), transition=AsyncMock(),
        )
        commands.bot = SimpleNamespace(user=SimpleNamespace(id=99))
        commands.welcome_messages = SimpleNamespace(
            create=AsyncMock(return_value="Welcome"),
        )

        with (
            patch("elbow_helper.features.support_tickets.commands.LEAD", {1}),
            patch(
                "elbow_helper.features.support_tickets.commands.fail",
                new=AsyncMock(),
            ) as send_failure,
            self.assertLogs(
                "elbow_helper.features.support_tickets.commands",
                level="ERROR",
            ),
        ):
            await SupportCommandMixin.open_ticket.callback(
                commands,
                interaction,
                member,
                "Help",
            )

        ticket_channel.delete.assert_awaited_once_with()
        send_failure.assert_awaited_once_with(interaction)


if __name__ == "__main__":
    unittest.main()
