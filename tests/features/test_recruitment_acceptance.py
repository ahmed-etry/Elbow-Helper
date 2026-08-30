from __future__ import annotations

import asyncio
import sqlite3
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

import discord

from elbow_helper.features.recruitment import commands
from elbow_helper.features.recruitment import trials
from elbow_helper.features.recruitment.commands import RecruitmentCommandMixin
from elbow_helper.features.recruitment.trials import TrialMixin
from elbow_helper.features.recruitment.trials import TrialStartResult


class RecruitmentAcceptanceTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _flow():
        applicant_role = SimpleNamespace(id=10)
        trial_role = SimpleNamespace(id=20)
        clan_role = SimpleNamespace(id=30)
        roles = {
            applicant_role.id: applicant_role,
            trial_role.id: trial_role,
            clan_role.id: clan_role,
        }

        guild = MagicMock()
        guild.get_role.side_effect = roles.get
        interaction = MagicMock()
        interaction.guild = guild
        interaction.user.id = 99
        interaction.response.is_done.return_value = True
        interaction.followup.send = AsyncMock()

        user = MagicMock()
        user.id = 42
        user.mention = "<@42>"
        user.guild = guild
        user.roles = [applicant_role]
        user.edit = AsyncMock()
        user.remove_roles = AsyncMock()
        user.add_roles = AsyncMock()

        target_channel = MagicMock()
        target_channel.id = 200
        target_channel.send = AsyncMock()

        owner = RecruitmentCommandMixin()
        owner.bot = MagicMock()
        owner.bot.get_channel.return_value = SimpleNamespace(mention="<#100>")
        owner.logger = MagicMock()
        owner.account_links = MagicMock()
        owner.account_links.lookup_players = AsyncMock(
            return_value=[
                {"player_tag": "#A", "player_name": "Alpha"},
                {"player_tag": "#B", "player_name": "Beta"},
            ]
        )
        owner.account_links.refresh_linked_boards = AsyncMock()
        owner.start_trial_for_accept = AsyncMock(
            return_value=TrialStartResult(started=True)
        )
        owner.achievement_rewards = MagicMock()
        owner.achievement_rewards.award_achievement = AsyncMock()

        return (
            owner,
            interaction,
            user,
            target_channel,
            applicant_role,
            trial_role,
            clan_role,
        )

    @staticmethod
    def _config():
        return patch.multiple(
            commands,
            APPLICANT_ROLE_ID=10,
            TRIAL_ROLE_ID=20,
            CLAN_INFO_BOARDS={
                "BEH": {
                    "channel_id": 100,
                    "link": "https://example.test/beh",
                    "clan_role": 30,
                }
            },
        )

    async def test_successful_acceptance_does_not_send_another_confirmation(self) -> None:
        (
            owner,
            interaction,
            user,
            target_channel,
            applicant_role,
            trial_role,
            clan_role,
        ) = self._flow()

        with self._config():
            await owner._perform_accept_flow(
                interaction,
                user=user,
                valid_clans=["BEH"],
                nickname="Ahmad",
                days=7,
                target_channel=target_channel,
                additional_notes=None,
                player_tags=["#A", "#B"],
            )

        user.remove_roles.assert_awaited_once_with(applicant_role)
        self.assertEqual(
            [call.args[0] for call in user.add_roles.await_args_list],
            [trial_role, clan_role],
        )
        owner.start_trial_for_accept.assert_awaited_once_with(
            target_channel,
            7,
            user.id,
        )
        owner.achievement_rewards.award_achievement.assert_awaited_once_with(
            user.id,
            "fresh_recruit",
        )
        interaction.followup.send.assert_not_awaited()

    async def test_acceptance_reports_each_failure_and_continues_other_steps(self) -> None:
        (
            owner,
            interaction,
            user,
            target_channel,
            _,
            _,
            clan_role,
        ) = self._flow()
        forbidden_response = MagicMock(status=403, reason="Forbidden")
        server_response = MagicMock(status=500, reason="Internal Server Error")
        user.edit.side_effect = discord.Forbidden(forbidden_response, "forbidden")
        user.add_roles.side_effect = [
            discord.Forbidden(forbidden_response, "forbidden"),
            None,
        ]
        owner.account_links.upsert_link.side_effect = [
            None,
            sqlite3.OperationalError("database is locked"),
        ]
        owner.account_links.refresh_linked_boards.side_effect = RuntimeError(
            "board unavailable"
        )
        target_channel.send.side_effect = discord.HTTPException(
            server_response,
            "send failed",
        )
        owner.start_trial_for_accept.return_value = TrialStartResult(started=False)
        owner.achievement_rewards.award_achievement.side_effect = (
            sqlite3.OperationalError("database is locked")
        )

        with self._config():
            await owner._perform_accept_flow(
                interaction,
                user=user,
                valid_clans=["BEH"],
                nickname="Ahmad",
                days=7,
                target_channel=target_channel,
                additional_notes=None,
                player_tags=["#A", "#B"],
            )

        self.assertEqual(user.edit.await_count, 1)
        self.assertIs(user.add_roles.await_args_list[1].args[0], clan_role)
        owner.start_trial_for_accept.assert_awaited_once()
        owner.achievement_rewards.award_achievement.assert_awaited_once()
        interaction.followup.send.assert_awaited_once_with(
            "Acceptance is incomplete for <@42>:\n"
            "- Nickname was not changed.\n"
            "- Trial role was not added.\n"
            "- Clash accounts were not linked: `#B`.\n"
            "- Welcome message was not posted.\n"
            "- Trial tracking was not started.\n"
            "- Fresh Recruit achievement was not awarded.\n"
            "\n"
            "Other acceptance steps were completed.",
            ephemeral=True,
        )

    async def test_failed_member_update_is_reported(self) -> None:
        owner = RecruitmentCommandMixin()
        owner.logger = MagicMock()
        response = MagicMock(status=503, reason="Service Unavailable")
        action = AsyncMock(side_effect=discord.HTTPException(response, "failed"))

        completed = await owner._apply_accept_member_update(
            action,
            label="Trial role addition",
            user_id=42,
        )

        self.assertFalse(completed)
        action.assert_awaited_once_with()

    async def test_ticket_rename_failure_is_reported(self) -> None:
        owner, interaction, user, target_channel, *_ = self._flow()
        owner.start_trial_for_accept.return_value = TrialStartResult(
            started=True,
            ticket_renamed=False,
        )

        with self._config():
            await owner._perform_accept_flow(
                interaction,
                user=user,
                valid_clans=["BEH"],
                nickname="Ahmad",
                days=7,
                target_channel=target_channel,
                additional_notes=None,
                player_tags=["#A", "#B"],
            )

        message = interaction.followup.send.await_args.args[0]
        self.assertIn("- Ticket was not renamed for the trial.", message)
        self.assertIn("Other acceptance steps were completed.", message)

    async def test_trial_state_failure_removes_the_untracked_message(self) -> None:
        owner = TrialMixin()
        owner._trial_lock = asyncio.Lock()
        owner.logger = MagicMock()
        tracking_message = MagicMock()
        tracking_message.id = 500
        tracking_message.delete = AsyncMock()
        tracking_channel = MagicMock()
        tracking_channel.id = 600
        tracking_channel.send = AsyncMock(return_value=tracking_message)
        owner.bot = MagicMock()
        owner.bot.get_channel.return_value = tracking_channel
        owner.state_store = MagicMock()
        owner.state_store.load_trial_data.return_value = {}
        owner.state_store.save_trial_data.side_effect = OSError("disk full")
        owner._build_trial_tracking_embed = MagicMock(return_value=MagicMock())
        channel = MagicMock()
        channel.id = 200
        channel.guild.id = 1
        channel.edit = AsyncMock()

        with self.assertRaises(OSError):
            await owner.start_trial_for_accept(channel, 7, 42)

        tracking_message.delete.assert_awaited_once_with()
        channel.edit.assert_not_awaited()

    async def test_trial_start_reports_when_the_ticket_cannot_be_renamed(self) -> None:
        owner = TrialMixin()
        owner._trial_lock = asyncio.Lock()
        owner.logger = MagicMock()
        tracking_message = MagicMock()
        tracking_message.id = 500
        tracking_channel = MagicMock()
        tracking_channel.id = 600
        tracking_channel.send = AsyncMock(return_value=tracking_message)
        owner.bot = MagicMock()
        owner.bot.get_channel.return_value = tracking_channel
        owner.state_store = MagicMock()
        owner.state_store.load_trial_data.return_value = {}
        owner._build_trial_tracking_embed = MagicMock(return_value=MagicMock())
        channel = MagicMock()
        channel.id = 200
        channel.guild.id = 1
        channel.edit = AsyncMock()

        with (
            patch.object(trials, "rename_ticket_channel", return_value="trial-ticket"),
            patch.object(trials, "can_rename", return_value=False),
        ):
            result = await owner.start_trial_for_accept(channel, 7, 42)

        self.assertEqual(
            result,
            TrialStartResult(started=True, ticket_renamed=False),
        )
        owner.state_store.save_trial_data.assert_called_once()
        channel.edit.assert_not_awaited()

    async def test_existing_trial_state_is_reused_without_another_message(self) -> None:
        owner = TrialMixin()
        owner._trial_lock = asyncio.Lock()
        owner.logger = MagicMock()
        tracking_channel = MagicMock()
        tracking_channel.send = AsyncMock()
        owner.bot = MagicMock()
        owner.bot.get_channel.return_value = tracking_channel
        owner.state_store = MagicMock()
        owner.state_store.load_trial_data.return_value = {
            "200": {"applicant_id": 42}
        }
        channel = MagicMock()
        channel.id = 200
        channel.guild.id = 1

        with patch.object(
            owner,
            "_rename_trial_ticket",
            new=AsyncMock(return_value=True),
        ) as rename:
            result = await owner.start_trial_for_accept(channel, 7, 42)

        self.assertEqual(result, TrialStartResult(started=True))
        tracking_channel.send.assert_not_awaited()
        rename.assert_awaited_once_with(channel)

    async def test_departed_trial_member_removes_tracking_before_state(self) -> None:
        owner = TrialMixin()
        owner._trial_lock = asyncio.Lock()
        owner.logger = MagicMock()
        trial_info = {
            "applicant_id": 42,
            "tracking_msg_id": 500,
            "tracking_channel_id": 600,
        }
        owner.state_store = MagicMock()
        owner.state_store.load_trial_data.side_effect = [
            {"200": dict(trial_info)},
            {"200": dict(trial_info)},
        ]
        owner._delete_tracking_message = AsyncMock(return_value=True)
        member = SimpleNamespace(id=42)

        await owner.on_member_remove(member)

        owner._delete_tracking_message.assert_awaited_once_with(
            trial_info,
            ticket_channel_id=200,
        )
        owner.state_store.save_trial_data.assert_called_once_with({})

    async def test_departed_trial_member_keeps_state_when_cleanup_fails(self) -> None:
        owner = TrialMixin()
        owner._trial_lock = asyncio.Lock()
        owner.logger = MagicMock()
        trial_info = {
            "applicant_id": 42,
            "tracking_msg_id": 500,
            "tracking_channel_id": 600,
        }
        owner.state_store = MagicMock()
        owner.state_store.load_trial_data.return_value = {
            "200": dict(trial_info)
        }
        owner._delete_tracking_message = AsyncMock(return_value=False)
        member = SimpleNamespace(id=42)

        await owner.on_member_remove(member)

        owner.state_store.save_trial_data.assert_not_called()
        owner.logger.warning.assert_called_once()

if __name__ == "__main__":
    unittest.main()
