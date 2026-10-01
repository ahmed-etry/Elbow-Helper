"""Recruitment decisions use their feature's prepared messages."""

from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.features.agent.actions.contracts import ActionClass
from elbow_helper.features.agent.commands.adapters.recruitment import (
    prepare_decline, prepare_finalize, recruitment_adapters,
)
from elbow_helper.features.recruitment.commands import RecruitmentCommandMixin


class RecruitmentDecisionTests(unittest.IsolatedAsyncioTestCase):
    async def test_finalize_previews_roles_ticket_and_message(self):
        workflow = RecruitmentCommandMixin()
        bot_member = SimpleNamespace(
            id=1, top_role=SimpleNamespace(position=10),
        )

        def role(role_id, name):
            return SimpleNamespace(
                id=role_id, name=name, mention=f"<@&{role_id}>",
                position=1, managed=False, permissions=SimpleNamespace(),
                is_default=lambda: False,
            )

        from elbow_helper.configuration.roles import (
            TRIAL_ROLE_ID, MEMBER_ROLE_ID, ALLIANCE_MEMBER_ROLE_ID,
        )
        roles = {
            TRIAL_ROLE_ID: role(TRIAL_ROLE_ID, "Trial"),
            MEMBER_ROLE_ID: role(MEMBER_ROLE_ID, "Member"),
            ALLIANCE_MEMBER_ROLE_ID: role(ALLIANCE_MEMBER_ROLE_ID, "Alliance"),
        }
        applicant = SimpleNamespace(
            id=3, mention="<@3>", display_name="Recruit",
            top_role=SimpleNamespace(position=1),
            roles=[roles[TRIAL_ROLE_ID]],
            remove_roles=AsyncMock(), add_roles=AsyncMock(),
        )
        channel = SimpleNamespace(
            id=4, mention="<#4>", name="🤔-trial", send=AsyncMock(),
            edit=AsyncMock(),
            permissions_for=lambda _: SimpleNamespace(
                view_channel=True, send_messages=True,
            ),
        )
        guild = SimpleNamespace(
            id=5, me=bot_member, get_role=lambda role_id: roles.get(role_id),
            get_member=lambda member_id: applicant if member_id == 3 else None,
            fetch_member=AsyncMock(return_value=applicant),
            get_channel_or_thread=lambda channel_id: channel if channel_id == 4 else None,
        )
        channel.guild = guild
        context = SimpleNamespace(
            bot=SimpleNamespace(get_cog=lambda _: workflow),
            guild=guild, member=SimpleNamespace(id=2),
            source_message=SimpleNamespace(channel=channel),
        )
        with patch("elbow_helper.features.recruitment.commands.discord.TextChannel",
                   new=SimpleNamespace):
            change = await prepare_finalize(context, {"applicant": 3})
            self.assertTrue(await change.preview.recheck())
            self.assertIn("Remove <@&" + str(TRIAL_ROLE_ID) + "> from <@3>.",
                          change.preview.lines)
            self.assertTrue(any("🤔-trial → ✅-trial" in line
                                for line in change.preview.lines))
            self.assertIs(next(adapter for adapter in recruitment_adapters()
                               if adapter.path == "/finalize").classification,
                          ActionClass.IRREVERSIBLE)
            channel.send.assert_not_awaited()
            result = await change.run()
        self.assertIn("Completed Recruit's trial", result.text)
        channel.send.assert_awaited_once()

    async def test_decline_previews_message_and_posts_on_confirm(self):
        workflow = RecruitmentCommandMixin()
        bot_member = SimpleNamespace(
            id=1, top_role=SimpleNamespace(position=10),
        )
        applicant = SimpleNamespace(
            id=3, mention="<@3>", display_name="Applicant",
            top_role=SimpleNamespace(position=1), roles=[],
        )
        channel = SimpleNamespace(
            id=4, mention="<#4>", name="recruitment-ticket",
            send=AsyncMock(), edit=AsyncMock(),
            permissions_for=lambda _: SimpleNamespace(
                view_channel=True, send_messages=True,
            ),
        )
        guild = SimpleNamespace(
            id=5, me=bot_member,
            get_member=lambda member_id: applicant if member_id == 3 else None,
            fetch_member=AsyncMock(return_value=applicant),
            get_channel_or_thread=lambda channel_id: channel if channel_id == 4 else None,
        )
        channel.guild = guild
        context = SimpleNamespace(
            bot=SimpleNamespace(get_cog=lambda _: workflow),
            guild=guild, member=SimpleNamespace(id=2),
            source_message=SimpleNamespace(channel=channel),
        )
        with patch("elbow_helper.features.recruitment.commands.discord.TextChannel",
                   new=SimpleNamespace):
            change = await prepare_decline(context, {
                "applicant": 3, "additional_notes": "Reason given.",
            })
            self.assertTrue(await change.preview.recheck())
            self.assertTrue(any("Reason given." in line
                                for line in change.preview.lines))
            self.assertIs(next(adapter for adapter in recruitment_adapters()
                               if adapter.path == "/decline").classification,
                          ActionClass.IRREVERSIBLE)
            channel.send.assert_not_awaited()
            result = await change.run()
        self.assertIn("Declined Applicant", result.text)
        channel.send.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
