"""Thumbs-down reports on exact archived agent answer identities."""

import asyncio
from datetime import datetime, timezone
import logging

import discord
from discord.ext import commands

from elbow_helper.configuration.guild import GUILD_ID, OWNER_DISCORD_ID
from elbow_helper.agent.safety.access import has_agent_entry_access


LOGGER = logging.getLogger(__name__)


def report_card(report):
    link = (f"https://discord.com/channels/{report['guild_id']}/"
            f"{report['channel_id']}/{report['reply_message_id']}")
    card = discord.Embed(title="Agent reply report")
    card.add_field(name="Reply", value=link, inline=False)
    card.add_field(name="Request", value=str(report["request_message_id"]))
    card.add_field(name="Reporter", value=f"<@{report['reporter_id']}>")
    for label, key, limit in (("Question", "question", 700), ("Answer", "delivered_answer", 1_000)):
        value = report[key]
        card.add_field(name=label, value=(value[:limit - 1] + "…" if len(value) > limit else value) or "None",
                       inline=False)
    diagnostics = report["diagnostics"]
    usage = diagnostics.get("usage") if diagnostics else None
    card.add_field(name="Rounds / tokens", value=(
        f"{usage['rounds']} / {usage['prompt_tokens']} + {usage['completion_tokens']}"
        + (" (partial)" if usage["unknown_token_rounds"] else "")
    ) if usage else "Unknown")
    tools = ", ".join(dict.fromkeys(call["capability"] for call in diagnostics["tool_calls"])) if diagnostics else "Unknown"
    card.add_field(name="Tools", value=(tools[:499] + "…" if len(tools) > 500 else tools) or "None", inline=False)
    return card


class AgentFeedbackMixin:
    def _archive_request_failure(self, request_id, error, terminal_cause):
        if self.transcript_archive is not None:
            self.transcript_archive.enqueue_write(self.transcript_archive.record_request_failure,
                request_id=request_id, error=error, terminal_cause=terminal_cause)

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload):
        if (payload.guild_id != GUILD_ID or payload.emoji.id is not None
                or payload.emoji.name != "👎" or self.transcript_archive is None):
            return
        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return
        member = guild.get_member(payload.user_id) or payload.member
        if (member is None or member.id != payload.user_id or getattr(member, "bot", True)
                or not has_agent_entry_access(member)):
            return
        reported_at = datetime.now(timezone.utc).isoformat()
        try:
            archive = self.transcript_archive
            await archive.drain()
            request_id = await asyncio.to_thread(archive.reply_request_id, guild_id=guild.id,
                                                channel_id=payload.channel_id, reply_id=payload.message_id)
            if request_id is None:
                request_id = next((identifier for identifier, context in self._active_contexts.items()
                    if context.guild.id == guild.id and context.source_message.channel.id == payload.channel_id
                    and payload.message_id in context.state.reply_delivery.delivery.message_ids), None)
            if request_id is None:
                return
            # A posted answer can be reacted to before its request has finished.
            # Wait for existing generation/accounting, without starting any work.
            finished = self._request_finished.get(request_id)
            if finished is not None:
                await finished.wait()
            report = await archive.report_reply(guild_id=guild.id, channel_id=payload.channel_id,
                                                reply_id=payload.message_id, reporter_id=member.id,
                                                reported_at=reported_at)
            if report is None:
                return
            LOGGER.info("Agent reply report stored: report=%s request=%s reply=%s reporter=%s",
                        report["report_id"], request_id, payload.message_id, member.id)
            await self._post_reply_report(report)
        except Exception:
            LOGGER.exception("Agent reply report failed: reply=%s reporter=%s", payload.message_id, member.id)

    async def _post_reply_report(self, report):
        try:
            owner = self.bot.get_user(OWNER_DISCORD_ID)
            if owner is None:
                owner = await self.bot.fetch_user(OWNER_DISCORD_ID)
            await owner.send(embed=report_card(report),
                             allowed_mentions=discord.AllowedMentions.none())
        except Exception:
            LOGGER.exception("Agent reply report owner DM failed: report=%s", report["report_id"])
