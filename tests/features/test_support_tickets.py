from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from elbow_helper.features.support_tickets.commands import SupportCommandMixin


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
    @staticmethod
    def _interaction(channel: _FakeTextChannel, guild: object) -> SimpleNamespace:
        return SimpleNamespace(
            guild=guild,
            channel=channel,
            user=SimpleNamespace(
                roles=[SimpleNamespace(id=1)],
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
        commands.build_confirm_view = MagicMock(return_value=SimpleNamespace())

        with (
            patch("elbow_helper.features.support_tickets.commands.LEAD", {1}),
            patch("elbow_helper.features.support_tickets.commands.RECRUITERS", set()),
            patch("elbow_helper.features.support_tickets.commands.SUPPORT_TRANSCRIPTS", 456),
            patch("elbow_helper.features.support_tickets.commands.discord.TextChannel", _FakeTextChannel),
            patch(
                "elbow_helper.features.support_tickets.commands.load_tickets",
                return_value={"123": {"source": "open"}},
            ),
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


if __name__ == "__main__":
    unittest.main()
