from __future__ import annotations

import asyncio
import sqlite3
from tests.temp_directory import TemporaryDirectory
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

import discord

from tests.data.links.link_fixtures import attach_links
from tests.features.profile_source import profile_demand
from elbow_helper.data.events.testing import MemoryEventsStore
from elbow_helper.features.account_links.cog import AccountLinks
from elbow_helper.features.recruitment import commands
from elbow_helper.features.recruitment import helpers
from elbow_helper.features.recruitment import trials
from elbow_helper.features.recruitment.commands import RecruitmentCommandMixin
from elbow_helper.features.recruitment.state import RecruitmentState
from elbow_helper.features.recruitment.trials import TrialMixin
from elbow_helper.features.recruitment.trials import TrialStartResult
from elbow_helper.infrastructure.clash import ClashResponse


class RecruitmentAcceptanceTests(unittest.IsolatedAsyncioTestCase):
    async def _state(self, trials=None, reminders=None):
        events = MemoryEventsStore()
        self.addCleanup(events.close)
        state = RecruitmentState(events)
        await state.startup()
        await state.change(lambda saved: saved.update(trials=trials or {}, reminders=reminders or {}) or ())
        return state

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
        owner.account_links.upsert_link = AsyncMock()
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
        owner.recruitment_state = SimpleNamespace(record_event=AsyncMock())

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


    async def test_accept_rejects_an_overlong_nickname_before_confirmation(self) -> None:
        owner, _, user, _, *_ = self._flow()
        owner.parse_clan_input = MagicMock(return_value=(["BEH"], []))
        channel = MagicMock(spec=discord.TextChannel, mention="<#200>")
        arguments = dict(user=user, clans="BEH", player_tags="#P0Y", days=7, channel=channel)

        with self._config():
            rejected = await owner.prepare_accept(nickname="x" * 33, **arguments)
            self.assertEqual(rejected["issue"], "Nicknames can be 32 characters at most.")
            self.assertNotIn("embed", rejected)
            owner.account_links.lookup_players.assert_not_awaited()

            accepted = await owner.prepare_accept(nickname="x" * 32, **arguments)
            self.assertIsNone(accepted["issue"])
            self.assertEqual(accepted["embed"].title, "Confirm Applicant Acceptance")

    async def test_accept_rejects_404_and_saves_temporary_failures_and_named_players(self) -> None:
        for prepared in (False, True):
            with self.subTest(prepared=prepared):
                owner, interaction, user, target_channel, *_ = self._flow()
                links = object.__new__(AccountLinks)
                tags = ["#P0Y", "#LQG", "#2PP", "#2PQ", "#2PR"]
                links.profiles = profile_demand(SimpleNamespace(
                    configured=True,
                    get=AsyncMock(side_effect=[
                        ClashResponse(404, {"reason": "notFound"}, {}, 1, 0),
                        ClashResponse(None, None, {}, 3, 0, TimeoutError()),
                        ClashResponse(503, {"reason": "maintenance"}, {}, 3, 0),
                        ClashResponse(429, {"reason": "requestThrottled"}, {}, 3, 0),
                        ClashResponse(200, {"name": "Alpha"}, {}, 1, 0),
                    ]),
                ))
                links.refresh_linked_boards = AsyncMock()
                owner.account_links = links

                with TemporaryDirectory() as directory:
                    with patch("elbow_helper.features.account_links.database.DB_PATH", Path(directory) / "links.db"):
                        links._init_db()
                        await attach_links(links)
                        with self._config():
                            arguments = dict(
                                user=user, valid_clans=["BEH"], nickname="Ahmad",
                                days=7, target_channel=target_channel,
                                additional_notes=None, player_tags=tags,
                            )
                            if prepared:
                                rows = await links.lookup_players(tags)
                                result = await owner.perform_accept(**arguments, player_rows=rows)
                                self.assertEqual(result["failures"], ("Clash accounts were not linked: `#P0Y`.",))
                            else:
                                await owner._perform_accept_flow(interaction, **arguments)
                                interaction.followup.send.assert_awaited_once_with(
                                    "Acceptance is incomplete for <@42>:\n"
                                    "- Clash accounts were not linked: `#P0Y`.\n"
                                    "\nOther acceptance steps were completed.",
                                    ephemeral=True,
                                )
                        saved = (await asyncio.to_thread(links.get_all_links))

                self.assertEqual(set(saved), set(tags[1:]))
                self.assertTrue(all(row["discord_user_id"] == user.id for row in saved.values()))
                for tag in tags[1:4]:
                    self.assertEqual(saved[tag]["player_name_last_seen"], tag)
                self.assertEqual(saved[tags[4]]["player_name_last_seen"], "Alpha")
                owner.start_trial_for_accept.assert_awaited_once()
                owner.achievement_rewards.award_achievement.assert_awaited_once()


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
        owner.recruitment_state = await self._state()
        owner.recruitment_state.events.fail_next_write = True
        owner._build_trial_tracking_embed = MagicMock(return_value=MagicMock())
        channel = MagicMock()
        channel.id = 200
        channel.guild.id = 1
        channel.edit = AsyncMock()

        with self.assertRaises(sqlite3.OperationalError):
            await owner.start_trial_for_accept(channel, 7, 42)

        tracking_message.delete.assert_awaited_once_with()
        channel.edit.assert_not_awaited()
        self.assertEqual(owner.recruitment_state.trials(), {})
        self.assertEqual(await owner.recruitment_state.events.read(), [])


    async def test_existing_trial_state_is_reused_without_another_message(self) -> None:
        owner = TrialMixin()
        owner._trial_lock = asyncio.Lock()
        owner.logger = MagicMock()
        tracking_channel = MagicMock()
        tracking_channel.send = AsyncMock()
        owner.bot = MagicMock()
        owner.bot.get_channel.return_value = tracking_channel
        owner.recruitment_state = await self._state({"200": {"applicant_id": 42}})
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

    async def test_rename_cap_is_per_ticket_and_failed_edits_do_not_use_it(self):
        for fail_first in (False, True):
            with self.subTest(fail_first=fail_first), patch.object(helpers, "channel_rename_timestamps", {}):
                owner = TrialMixin()
                owner.logger = MagicMock()
                channel = SimpleNamespace(
                    id=200, guild=SimpleNamespace(id=1),
                    name=trials.TRIAL_TICKET_PREFIXES_START[0][0] + "member", edit=AsyncMock(),
                )
                if fail_first:
                    channel.edit.side_effect = discord.Forbidden(
                        SimpleNamespace(status=403, reason="Forbidden"), "denied")
                    for _ in range(2):
                        self.assertFalse(await owner._rename_trial_ticket(channel))
                    channel.edit.side_effect = None
                self.assertTrue(await owner._rename_trial_ticket(channel))
                self.assertTrue(await owner._rename_trial_ticket(channel))
                self.assertFalse(await owner._rename_trial_ticket(channel))
                other = SimpleNamespace(id=201, guild=channel.guild, name=channel.name, edit=AsyncMock())
                self.assertTrue(await owner._rename_trial_ticket(other))
                self.assertEqual(channel.edit.await_count, 4 if fail_first else 2)
                other.edit.assert_awaited_once()

    async def test_departed_trial_member_removes_tracking_before_state(self) -> None:
        owner = TrialMixin()
        owner._trial_lock = asyncio.Lock()
        owner.logger = MagicMock()
        trial_info = {
            "applicant_id": 42,
            "tracking_msg_id": 500,
            "tracking_channel_id": 600,
        }
        owner.recruitment_state = await self._state({"200": dict(trial_info)})
        owner._delete_tracking_message = AsyncMock(return_value=True)
        member = SimpleNamespace(id=42)

        await owner.on_member_remove(member)

        owner._delete_tracking_message.assert_awaited_once_with(
            trial_info,
            ticket_channel_id=200,
        )
        self.assertEqual(owner.recruitment_state.trials(), {})

    async def test_departed_trial_member_keeps_state_when_cleanup_fails(self) -> None:
        owner = TrialMixin()
        owner._trial_lock = asyncio.Lock()
        owner.logger = MagicMock()
        trial_info = {
            "applicant_id": 42,
            "tracking_msg_id": 500,
            "tracking_channel_id": 600,
        }
        owner.recruitment_state = await self._state({"200": dict(trial_info)})
        owner._delete_tracking_message = AsyncMock(return_value=False)
        member = SimpleNamespace(id=42)

        await owner.on_member_remove(member)

        self.assertEqual(owner.recruitment_state.trials(), {"200": trial_info})
        owner.logger.warning.assert_called_once()

    async def test_end_trial_keeps_state_when_the_ticket_is_unavailable(self) -> None:
        owner = TrialMixin()
        owner._trial_lock = asyncio.Lock()
        owner.bot = MagicMock()
        owner.bot.get_channel.return_value = None
        owner.recruitment_state = await self._state({"200": {"applicant_id": 42}})
        interaction = SimpleNamespace(
            user=SimpleNamespace(roles=[SimpleNamespace(id=1)]),
            response=SimpleNamespace(
                is_done=MagicMock(return_value=False),
                defer=AsyncMock(),
            ),
            followup=SimpleNamespace(send=AsyncMock()),
        )

        with patch.object(trials, "CORE", frozenset({1})):
            ended = await owner.end_trial_now(interaction, 200, 42)

        self.assertFalse(ended)
        self.assertEqual(owner.recruitment_state.trials(), {"200": {"applicant_id": 42}})

    async def test_failed_trial_followup_stays_pending_until_another_end(self):
        for expired in (False, True):
            with self.subTest(expired=expired):
                owner = TrialMixin()
                owner._trial_lock, owner._reminder_lock = asyncio.Lock(), asyncio.Lock()
                owner.logger = MagicMock()
                info = {"start": "2020-01-01T00:00:00+00:00", "days": 7, "applicant_id": 42,
                        "tracking_msg_id": 500, "tracking_channel_id": 600}
                reminder = {"channel_id": 800, "message_id": 700, "applicant_id": 42,
                            "created_at": 1577836800, "resolved_at": None}
                owner.recruitment_state = await self._state(
                    trials={} if expired else {"200": info},
                    reminders={"200": reminder} if expired else {},
                )
                channel = SimpleNamespace(
                    id=200, name=trials.TRIAL_TICKET_PREFIXES_END[0][0] + "member",
                    guild=SimpleNamespace(id=1), edit=AsyncMock(),
                    send=AsyncMock(side_effect=discord.HTTPException(
                        SimpleNamespace(status=500, reason="Server Error"), "down")),
                )
                tracking_message = SimpleNamespace(delete=AsyncMock())
                tracking_channel = MagicMock(spec=discord.TextChannel)
                tracking_channel.fetch_message = AsyncMock(side_effect=[
                    tracking_message,
                    discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "gone"),
                ])

                async def no_history(**_):
                    return
                    yield

                tracking_channel.history = no_history
                owner.bot = SimpleNamespace(get_channel={200: channel, 600: tracking_channel}.get)
                channel.fetch_message = AsyncMock(return_value=SimpleNamespace(id=700, edit=AsyncMock()))
                owner._build_trial_reminder_embed = MagicMock(return_value=None)
                actor = SimpleNamespace(id=7, roles=[SimpleNamespace(id=1)])
                with patch.object(trials, "CORE", frozenset({1})), patch.object(helpers, "channel_rename_timestamps", {}):
                    result = await owner.complete_trial_end(
                        200, 42, actor, allow_missing=expired, resolve_reminder=expired)
                    self.assertFalse(result.ended)
                    self.assertIn("couldn't post the trial follow-up", result.error)
                    channel.edit.assert_not_awaited()
                    self.assertEqual(owner.recruitment_state.trials(), {} if expired else {"200": info})
                    self.assertFalse(owner.recruitment_state.reminders().get("200", {}).get("resolved_at"))
                    self.assertEqual(await owner.recruitment_state.events.read(), [])
                    channel.send.side_effect = None
                    result = await owner.complete_trial_end(
                        200, 42, actor, allow_missing=expired, resolve_reminder=expired)
                self.assertTrue(result.ended)
                self.assertEqual(owner.recruitment_state.trials(), {})
                self.assertEqual(channel.send.await_count, 2)
                self.assertEqual(len(await owner.recruitment_state.events.read()), 1)
                channel.edit.assert_awaited_once()
                if not expired:
                    tracking_message.delete.assert_awaited_once()

    async def test_trial_ends_with_a_notice_when_the_bot_cannot_send_in_the_ticket(self):
        owner = TrialMixin()
        owner._trial_lock, owner._reminder_lock = asyncio.Lock(), asyncio.Lock()
        owner.logger = MagicMock()
        info = {"start": "2020-01-01T00:00:00+00:00", "days": 7, "applicant_id": 42}
        owner.recruitment_state = await self._state(trials={"200": info})
        channel = SimpleNamespace(
            id=200, name="ticket-member", guild=SimpleNamespace(id=1), edit=AsyncMock(),
            send=AsyncMock(side_effect=discord.Forbidden(
                SimpleNamespace(status=403, reason="Forbidden"), "denied")),
        )
        owner.bot = SimpleNamespace(get_channel=lambda _: channel)
        owner._delete_tracking_message = AsyncMock(return_value=True)
        actor = SimpleNamespace(id=7, roles=[SimpleNamespace(id=1)])
        with patch.object(trials, "CORE", frozenset({1})):
            result = await owner.complete_trial_end(200, 42, actor)
        self.assertTrue(result.ended)
        self.assertIn("can't send messages in the ticket", " ".join(result.notices))
        self.assertEqual(owner.recruitment_state.trials(), {})

    async def test_expired_trial_remains_active_when_reminder_state_fails(self) -> None:
        owner = TrialMixin()
        owner._trial_lock = asyncio.Lock()
        owner._reminder_lock = asyncio.Lock()
        owner.logger = MagicMock()
        trial_info = {
            "start": "2020-01-01T00:00:00+00:00",
            "days": 7,
            "applicant_id": 42,
            "tracking_msg_id": 500,
            "tracking_channel_id": 600,
        }
        owner.recruitment_state = await self._state({"200": dict(trial_info), "201": dict(trial_info)})
        owner.recruitment_state.events.fail_next_write = True
        owner._delete_tracking_message = AsyncMock(return_value=True)
        owner._build_trial_reminder_embed = MagicMock(return_value=MagicMock())

        reminder_message = SimpleNamespace(id=700, delete=AsyncMock())
        reminder_channel = SimpleNamespace(id=800, send=AsyncMock(return_value=reminder_message))
        ticket_channel = SimpleNamespace(id=200, mention="<#200>")
        owner.bot = MagicMock()
        owner.bot.get_channel.side_effect = lambda channel_id: (
            reminder_channel if channel_id == trials.REC_ROOM else ticket_channel
        )

        await TrialMixin.check_expired_trials.coro(owner)

        self.assertEqual(owner.recruitment_state.trials(), {"200": trial_info})
        self.assertEqual(set(owner.recruitment_state.reminders()), {"201"})
        owner._delete_tracking_message.assert_awaited_once_with(trial_info, ticket_channel_id=201)
        reminder_message.delete.assert_awaited_once_with()

    async def test_unexpected_error_in_one_expired_trial_does_not_stop_the_sweep(self) -> None:
        owner = TrialMixin()
        owner._trial_lock = asyncio.Lock()
        owner._reminder_lock = asyncio.Lock()
        owner.logger = MagicMock()
        trial_info = {"start": "2020-01-01T00:00:00+00:00", "days": 7, "applicant_id": 42}
        owner.recruitment_state = await self._state({"200": dict(trial_info), "201": dict(trial_info)})
        owner._delete_tracking_message = AsyncMock(side_effect=[AttributeError("odd entry"), True])
        owner._build_trial_reminder_embed = MagicMock(return_value=MagicMock())
        reminder_channel = SimpleNamespace(
            id=800, send=AsyncMock(return_value=SimpleNamespace(id=700, delete=AsyncMock())))
        owner.bot = MagicMock()
        owner.bot.get_channel.side_effect = lambda channel_id: (
            reminder_channel if channel_id == trials.REC_ROOM
            else SimpleNamespace(id=channel_id, mention=f"<#{channel_id}>"))

        await TrialMixin.check_expired_trials.coro(owner)

        self.assertEqual(owner.recruitment_state.trials(), {"200": trial_info})
        owner.logger.exception.assert_called_once()
        self.assertEqual(owner.logger.exception.call_args.args[1], 200)

    async def test_expired_trial_ends_when_its_ticket_was_deleted(self) -> None:
        owner = TrialMixin()
        owner._trial_lock = asyncio.Lock()
        owner._reminder_lock = asyncio.Lock()
        owner.logger = MagicMock()
        trial_info = {"start": "2020-01-01T00:00:00+00:00", "days": 7, "applicant_id": 42}
        owner.recruitment_state = await self._state({"200": dict(trial_info), "201": dict(trial_info)})
        owner._delete_tracking_message = AsyncMock(return_value=True)
        reminder_channel = SimpleNamespace(id=800, send=AsyncMock())
        owner.bot = MagicMock()
        owner.bot.get_channel.side_effect = lambda channel_id: (
            reminder_channel if channel_id == trials.REC_ROOM else None)
        missing = discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "gone")
        failing = discord.HTTPException(SimpleNamespace(status=503, reason="Unavailable"), "busy")

        def fetch_channel(channel_id):
            raise missing if channel_id == 200 else failing
        owner.bot.fetch_channel = AsyncMock(side_effect=fetch_channel)

        await TrialMixin.check_expired_trials.coro(owner)

        # Deleted ticket: trial ends. Discord not answering: trial kept for next hour.
        self.assertEqual(owner.recruitment_state.trials(), {"201": trial_info})
        reminder_channel.send.assert_not_awaited()

if __name__ == "__main__":
    unittest.main()
