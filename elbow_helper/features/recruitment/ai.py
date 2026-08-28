"""AI-assisted applicant summary workflows."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import discord
from discord.ext import tasks
from elbow_helper.configuration.channels import REC_ROOM
from elbow_helper.configuration.channels import RECRUITMENT_TICKET_CATEGORY
from elbow_helper.configuration.style import DEFAULT_EMBED_COLOR_HEX
from elbow_helper.configuration.style import DEFAULT_THUMBNAIL_URL
from elbow_helper.infrastructure.ai import TextGenerationError

from .config import APPLICANT_AI_CLEANUP_HOURS

LOGGER = logging.getLogger(__name__)
RECRUITMENT_REVIEW_MODEL = "gpt-5.4"
class AIMixin:

    @staticmethod
    def _chunk_ai_message(message: str, max_len: int = 2000) -> list[str]:
        if len(message) <= max_len:
            return [message]

        chunks: list[str] = []
        remaining = message
        while remaining:
            if len(remaining) <= max_len:
                chunks.append(remaining)
                break

            split_at = remaining.rfind("\n", 0, max_len)
            if split_at <= 0:
                split_at = remaining.rfind(" ", 0, max_len)
            if split_at <= 0:
                split_at = max_len

            chunk = remaining[:split_at].rstrip()
            if not chunk:
                chunk = remaining[:max_len]
                split_at = len(chunk)
            chunks.append(chunk)
            remaining = remaining[split_at:].lstrip()

        return chunks

    @staticmethod
    def _extract_application_answers(first_message: discord.Message) -> str:
        """Extract the structured application answers from the ticket opener message."""
        if not first_message.embeds or len(first_message.embeds) <= 1:
            return ""

        embed = first_message.embeds[1]
        parts: list[str] = []
        if getattr(embed, "description", None):
            parts.append(embed.description)
        if getattr(embed, "fields", None):
            for field in embed.fields:
                parts.append(f"{field.name}: {field.value}")
        return "\n".join(part for part in parts if part).strip()

    @staticmethod
    def _render_ticket_message(msg: discord.Message) -> str:
        """Render a ticket message into text, including attachment-only messages."""
        content = (msg.content or "").strip()
        attachment_names = ", ".join(attachment.filename for attachment in msg.attachments)
        if content and attachment_names:
            return f"{content} [attachments: {attachment_names}]"
        if content:
            return content
        if attachment_names:
            return f"[attachments: {attachment_names}]"
        return ""

    async def _build_ticket_second_opinion(
        self,
        ticket_channel: discord.TextChannel,
    ) -> list[str] | None:
        messages = [msg async for msg in ticket_channel.history(limit=100, oldest_first=True)]
        if not messages:
            return None

        first_msg = messages[0]
        first_line = first_msg.content.splitlines()[0] if first_msg.content else ""
        applicant_name = first_line.split()[0] if first_line else "Unknown"
        application_answers = self._extract_application_answers(first_msg)

        conversation_lines: list[str] = []
        for msg in messages:
            if msg.author.bot:
                continue
            content = self._render_ticket_message(msg)
            if not content:
                continue
            conversation_lines.append(f"{msg.author.display_name}: {content}")

        conversation_text = "\n".join(conversation_lines).strip()
        if not application_answers and not conversation_text:
            return None

        application_section = (
            application_answers
            if application_answers
            else "No application answers were found in the ticket."
        )
        conversation_section = (
            conversation_text
            if conversation_text
            else "No applicant conversation was found in the ticket."
        )

        prompt = f"""You are reviewing a Clash of Clans recruitment ticket to help staff make a decision.

Your job is to produce a clear decision aid grounded only in the evidence provided, not a generic opinion.

Applicant: {applicant_name}

Application answers:
{application_section}

Ticket conversation:
{conversation_section}

Evaluate the applicant on:
- effort and seriousness
- communication quality and clarity
- attitude toward leadership and clan expectations
- signs of reliability, fit, or likely friction

Use one recommendation only:
- Strong Accept
- Accept
- Borderline
- Decline

Rules:
- Base everything only on the answers and ticket conversation above.
- Do not invent background, skill, or intent that is not shown.
- If evidence is mixed or incomplete, prefer Borderline over forcing a stronger call.
- Keep the tone direct and recruiter-facing.
- Do not mention that you are an AI.
- Avoid filler, disclaimers, and generic praise.

Output exactly in this structure:

Recommendation: **<one of the 4 options>**
Confidence: **High / Medium / Low**

Why:
- bullet
- bullet
- bullet

Green Flags:
- bullet
- bullet

Risks:
- bullet
- bullet

Clarify Before Deciding:
- bullet
- bullet

Keep it concise but useful. Total response should stay under 220 words.
"""

        try:
            response_text = await self.text_generator.complete(
                model=RECRUITMENT_REVIEW_MODEL,
                prompt=prompt,
                temperature=0.2,
                max_completion_tokens=650,
            )
        except TextGenerationError as exc:
            raise RuntimeError("Recruitment AI request failed") from exc

        if not response_text:
            raise RuntimeError("Recruitment AI returned no content")
        response_message = f"AI Second Opinion for {ticket_channel.mention}\n{response_text}"
        return self._chunk_ai_message(response_message)

    async def _add_applicant_ai_message(self, message_id: int, channel_id: int) -> None:
        # Persist AI summary message metadata for deferred cleanup.
        async with self._applicant_ai_lock:
            self.applicant_ai_messages[str(message_id)] = {
                "channel_id": channel_id,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            self.state_store.save_applicant_ai_messages(
                self.applicant_ai_messages
            )

    async def _cleanup_applicant_ai_messages(self) -> None:
        # Remove AI summary messages that have exceeded retention.
        async with self._applicant_ai_lock:
            messages_to_cleanup = dict(self.applicant_ai_messages)
        if not messages_to_cleanup:
            return
        current_time = datetime.now(timezone.utc)
        cutoff_time = current_time - timedelta(hours=APPLICANT_AI_CLEANUP_HOURS)
        messages_to_remove = []
        for msg_id_str, data in messages_to_cleanup.items():
            try:
                created_at_str = data.get("created_at")
                if not created_at_str:
                    messages_to_remove.append(msg_id_str)
                    continue
                created_at = datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
                if created_at >= cutoff_time:
                    continue
                msg_id = int(msg_id_str)
                channel_id = data.get("channel_id")
                if channel_id:
                    channel = self.bot.get_channel(int(channel_id))
                    if channel:
                        try:
                            message = await channel.fetch_message(msg_id)
                            await message.delete()
                        except discord.NotFound:
                            self.logger.debug(
                                "AI summary message already missing during cleanup: message_id=%s channel_id=%s",
                                msg_id,
                                channel_id,
                            )
                        except discord.Forbidden:
                            self.logger.warning(
                                "Missing permissions deleting AI summary message: message_id=%s channel_id=%s",
                                msg_id,
                                channel_id,
                            )
                        except discord.HTTPException as e:
                            self.logger.warning(
                                "Error deleting AI summary message %s in channel %s: %s",
                                msg_id,
                                channel_id,
                                e,
                            )
                        except (TypeError, ValueError) as e:
                            self.logger.exception(
                                "Unexpected error deleting AI summary message %s in channel %s: %s",
                                msg_id,
                                channel_id,
                                e,
                            )
                messages_to_remove.append(msg_id_str)
            except (TypeError, ValueError, KeyError) as e:
                self.logger.exception("Unexpected error processing AI cleanup for %s: %s", msg_id_str, e)
                messages_to_remove.append(msg_id_str)
        if messages_to_remove:
            async with self._applicant_ai_lock:
                for msg_id_str in messages_to_remove:
                    self.applicant_ai_messages.pop(msg_id_str, None)
                self.state_store.save_applicant_ai_messages(
                    self.applicant_ai_messages
                )

    @tasks.loop(hours=6)
    async def cleanup_applicant_ai(self):
        await self._cleanup_applicant_ai_messages()

    @cleanup_applicant_ai.before_loop
    async def before_cleanup_applicant_ai(self):
        await self.bot.wait_until_ready()

    async def _process_applicant_ticket(self, channel: discord.TextChannel) -> None:
        if getattr(channel, "category_id", None) != RECRUITMENT_TICKET_CATEGORY:
            return
        try:
            await asyncio.sleep(3)
            await asyncio.sleep(2)
            messages = [msg async for msg in channel.history(limit=1, oldest_first=True)]
            if not messages:
                return
            first_msg = messages[0]
            content = self._extract_application_answers(first_msg)
            if not content.strip():
                return
            prompt = f"""You are a recruiter bot evaluating 5 short answers from a Clash of Clans applicant to help leadership. Grade with words, not numbers.

Grade only from the provided answers (no follow-ups). Use these tiers and stay consistent:
- Excellent: Thoughtful, specific, cooperative; clear effort and intent to contribute across answers.
- Strong: Polite and engaged; gives context or intent; concise but shows effort and fit.
- Good: Clear intent to participate; mostly positive tone; some brevity is fine if intent is present.
- Borderline: Very brief or generic but not dismissive; intent unclear; needs clarification before accepting.
- Weak: Noticeably low effort or thin answers, but still enough meaningful signal to continue with caution.
- Reject: Broadly non-responsive, placeholder, copy-paste, nonsense, dismissive, mocking, off-topic, or toxic answers; or so little meaningful effort that the application is unusable on its own.

Rules to keep:
- Judge effort, tone, fit for clan activities (CWL, Clan Games, Raids). Do NOT penalize controversial views unless phrased rudely or mockingly.
- Don't reward length alone; reward clarity/effort. One weak answer is forgivable if the rest show effort.
- Use the full range: if effort is clear, lean higher (Strong/Excellent); reserve Borderline/Weak/Reject for clear lack of effort, unusable answers, or bad tone.

Questionnaire answers:
{content}

Output exactly:
Overall: **<Tier>**

Feedback:

- bullet 1
- bullet 2 (optional)
- bullet 3 (optional)
Max 3 bullets, total <=60 words, concise, no intro/outro.
"""

            try:
                feedback = await self.text_generator.complete(
                    model=RECRUITMENT_REVIEW_MODEL,
                    prompt=prompt,
                    temperature=0.3,
                )
            except TextGenerationError as exc:
                self.logger.warning("Applicant review AI request failed: channel_id=%s error=%s", channel.id, exc)
                return

            if not feedback:
                return

            summary_channel = self.bot.get_channel(REC_ROOM)
            if summary_channel:
                ai_embed = discord.Embed(
                    title="Applicant Review",
                    description=f"Review for {channel.mention}:\n{feedback}",
                    color=discord.Color(DEFAULT_EMBED_COLOR_HEX),
                )
                ai_embed.set_thumbnail(url=DEFAULT_THUMBNAIL_URL)
                ai_embed.timestamp = datetime.now(timezone.utc)
                message = await summary_channel.send(embed=ai_embed)
                await self._add_applicant_ai_message(message.id, summary_channel.id)
        except (
            discord.Forbidden,
            discord.HTTPException,
            RuntimeError,
            ValueError,
            TypeError,
        ):
            self.logger.exception("Failed processing applicant ticket")
