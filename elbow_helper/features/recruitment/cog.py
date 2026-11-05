"""Main recruitment cog lifecycle and wiring."""

from __future__ import annotations

import asyncio
import logging
import time

import discord
from discord.ext import commands
from elbow_helper.core.background import start_resilient_loop
from elbow_helper.configuration.guild import GUILD_ID

from .ai import AIMixin
from .commands import RecruitmentCommandMixin
from .helpers import HelperMixin
from .state import RecruitmentState
from .queries import RecruitmentQueries
from .tickets import TicketMixin
from .trials import TrialMixin
from .views import PersistentEndNowView
from .views import PersistentEndTrialView


class Recruitment(HelperMixin, TrialMixin, TicketMixin, AIMixin, RecruitmentCommandMixin, commands.Cog):

    def __init__(
        self,
        bot,
        *,
        account_links,
        achievement_rewards,
        recruitment_state: RecruitmentState,
    ):
        self.bot = bot
        self.account_links = account_links
        self.achievement_rewards = achievement_rewards
        self.recruitment_state = recruitment_state
        self.queries = RecruitmentQueries(
            recruitment_state.trials,
            reminder_loader=recruitment_state.reminders,
            ticket_activity_loader=recruitment_state.ticket_activity_records,
        )
        self.text_generator = bot.text_generator
        self.logger = logging.getLogger(__name__)
        self._recurring_issue_log_times: dict[str, float] = {}
        # State/locks for persistent views and workflow data.
        self._persistent_views_added = False
        self._trial_lock = asyncio.Lock()
        self._reminder_lock = asyncio.Lock()
        self._ticket_reminder_lock = asyncio.Lock()
        self._applicant_ai_lock = asyncio.Lock()
        self._applicant_ticket_tasks: set[asyncio.Task[None]] = set()
        self.ticket_reminders = {}
        self.applicant_ai_messages = {}

        self.validate_clan_info_boards()

    async def cog_load(self):
        await self.recruitment_state.startup()
        self.ticket_reminders = self.recruitment_state.ticket_activity()
        self.applicant_ai_messages = self.recruitment_state.ai_messages()
        # Background maintenance loops.
        start_resilient_loop(self.check_expired_trials)
        start_resilient_loop(self.cleanup_trial_reminders)
        start_resilient_loop(self.organize_tickets)
        start_resilient_loop(self.check_inactive_tickets)
        start_resilient_loop(self.cleanup_old_ticket_reminders)
        start_resilient_loop(self.cleanup_applicant_ai)

    def _warn_recurring_issue(
        self,
        key: str,
        message: str,
        *args,
        cooldown_seconds: float = 3600.0,
    ) -> None:
        # Surface recurring task blockers at warning level, then downgrade repeats.
        now = time.monotonic()
        last_logged = self._recurring_issue_log_times.get(key)
        if last_logged is None or (now - last_logged) >= cooldown_seconds:
            self._recurring_issue_log_times[key] = now
            self.logger.warning(message, *args)
            return
        self.logger.debug(message, *args)

    def _clear_recurring_issue(self, *keys: str) -> None:
        for key in keys:
            self._recurring_issue_log_times.pop(key, None)

    def _warn_ticket_reorder_issue(self, key: str, message: str, *args) -> None:
        self._warn_recurring_issue(f"ticket_reorder:{key}", message, *args)

    def _clear_ticket_reorder_issue(self, *keys: str) -> None:
        self._clear_recurring_issue(*(f"ticket_reorder:{key}" for key in keys))

    def _start_applicant_ticket_processing(self, channel: discord.TextChannel) -> None:
        task = asyncio.create_task(
            self._process_applicant_ticket(channel),
            name=f"recruitment-ticket:{channel.id}",
        )
        self._applicant_ticket_tasks.add(task)

        def finish(completed: asyncio.Task[None]) -> None:
            self._applicant_ticket_tasks.discard(completed)
            try:
                completed.result()
            except asyncio.CancelledError:
                return
            except Exception:
                self.logger.exception(
                    "Applicant ticket processing failed for channel %s",
                    channel.id,
                )

        task.add_done_callback(finish)


    def cog_unload(self):
        self.recruitment_state.events.unregister_member_deletion("recruitment")
        self.check_expired_trials.cancel()
        self.cleanup_trial_reminders.cancel()
        self.organize_tickets.cancel()
        self.check_inactive_tickets.cancel()
        self.cleanup_old_ticket_reminders.cancel()
        self.cleanup_applicant_ai.cancel()
        for task in tuple(self._applicant_ticket_tasks):
            if not task.done():
                task.cancel()


    @commands.Cog.listener()
    async def on_ready(self):
        if self._persistent_views_added:
            return
        # Re-register persistent views after restart.
        trial_data = self.recruitment_state.trials()
        for channel_id, info in trial_data.items():
            applicant_id = info.get("applicant_id")
            if not applicant_id:
                continue
            try:
                self.bot.add_view(PersistentEndNowView(GUILD_ID, int(channel_id), int(applicant_id)))
            except (TypeError, ValueError, discord.HTTPException):
                self.logger.exception("Failed to register End Now view for %s", channel_id)
        reminders = self.recruitment_state.reminders()
        for ticket_id, info in reminders.items():
            applicant_id = info.get("applicant_id")
            message_id = info.get("message_id")
            if not applicant_id:
                channel = self.bot.get_channel(int(ticket_id))
                if isinstance(channel, discord.TextChannel):
                    applicant_id = await self._resolve_applicant_id(channel)
                    if applicant_id:
                        async with self._reminder_lock:
                            def edit(state, ticket_id=str(ticket_id), applicant_id=applicant_id):
                                current = state["reminders"].get(ticket_id)
                                if not isinstance(current, dict):
                                    return None
                                current["applicant_id"] = applicant_id
                                return ()
                            await self.recruitment_state.change(edit)
            if not applicant_id:
                continue
            try:
                view = PersistentEndTrialView(int(ticket_id), int(applicant_id))
                if message_id:
                    self.bot.add_view(view, message_id=int(message_id))
                else:
                    self.bot.add_view(view)
            except (TypeError, ValueError, discord.HTTPException):
                self.logger.exception("Failed to register End Trial view for %s", ticket_id)
        self._persistent_views_added = True

