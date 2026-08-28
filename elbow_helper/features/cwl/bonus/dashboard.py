"""CWL bonus dashboard state and helpers."""

from __future__ import annotations

import logging
import re
import io
import html
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone as dt_timezone
from typing import Any
from typing import Dict
from typing import List
from typing import Optional

import discord
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from elbow_helper.configuration.clans import CLAN_NAMES
from elbow_helper.configuration.guild import GUILD_ID
from elbow_helper.configuration.roles import HELPER_CWL_BASE_ROLE_ID
from elbow_helper.configuration.roles import MEMBERS
from elbow_helper.configuration.style import DEFAULT_EMBED_COLOR_HEX
from elbow_helper.configuration.style import DEFAULT_THUMBNAIL_URL
from ..config import BONUS_THREADS
from ..config import CWL_BONUS_ECONOMY_ENABLED
from ..config import CWL_HQ_CHANNEL_ID


LOGGER = logging.getLogger(__name__)

BONUS_BOARD_TITLE = "CWL Bonus Board"
BONUS_BOARD_DESCRIPTION = (
    "Use this board to send CWL coin and raffle-ticket rewards.\n\n"
    "After bonuses are assigned, post the member list in your clan's CWL Bonuses thread, then select your clan here."
)
BONUS_BOARD_FOOTER = "Post the list in your CWL Bonuses thread first."
BONUS_STATUS_NOT_STARTED = "not_started"
BONUS_STATUS_READY = "ready"
BONUS_STATUS_COMPLETED = "completed"
BONUS_STATUS_NEEDS_REVIEW = "needs_review"
BONUS_STATUS_ON_HOLD = "on_hold"
BONUS_STATUS_SKIPPED = "skipped"
BONUS_BOARD_MODE_REVIEW = "review"
BONUS_BOARD_MODE_FINAL = "final"
BONUS_CLAN_ORDER = ("BEH", "BE4", "BES", "BE1", "BEM", "BEC", "BEP", "BEE")
BONUS_CLAN_COLORS = {
    "BEH": {"accent": "#b52e2e", "soft": "#f6d9d9"},
    "BE4": {"accent": "#ac5414", "soft": "#f4dfcf"},
    "BES": {"accent": "#604d48", "soft": "#e7e1df"},
    "BE1": {"accent": "#975c4f", "soft": "#efe0db"},
    "BEM": {"accent": "#3e83ae", "soft": "#d8e8f2"},
    "BEC": {"accent": "#e09f55", "soft": "#faebd8"},
    "BEP": {"accent": "#4b6950", "soft": "#dce8de"},
    "BEE": {"accent": "#988007", "soft": "#f2edc9"},
}


@dataclass(slots=True)
class BonusCandidate:
    month_key: int
    month_label: str
    clan_code: str
    source_type: str
    source_message_id: Optional[int]
    source_channel_id: Optional[int]
    source_url: Optional[str]
    source_author_id: Optional[int]
    source_author_name: Optional[str]
    source_created_at: Optional[int]
    source_text: str
    recipient_ids: List[int]


@dataclass(slots=True)
class BonusGrantSummary:
    reward_kind: str
    granted: List[str]
    skipped: List[str]
    elder_granted: List[str]
    member_granted: List[str]
    recipient_count: int


class CwlBonusDashboardMixin:
    def _bonus_month_key(self) -> int:
        now = datetime.now(dt_timezone.utc)
        return now.year * 12 + now.month

    def _bonus_month_label(self, month_key: Optional[int] = None) -> str:
        month_key = month_key or self._bonus_month_key()
        year = (month_key - 1) // 12
        month = month_key - (year * 12)
        return datetime(year, month, 1, tzinfo=dt_timezone.utc).strftime("%B %Y")

    def _bonus_board_key(self, mode: str, month_key: Optional[int] = None) -> str:
        return f"{mode}:{month_key or self._bonus_month_key()}"

    def _review_board_key(self, month_key: Optional[int] = None) -> str:
        return self._bonus_board_key(BONUS_BOARD_MODE_REVIEW, month_key)

    def _default_bonus_clan_state(self) -> Dict[str, Any]:
        return {
            "status": BONUS_STATUS_NOT_STARTED,
            "last_actor_id": None,
            "completed_by_id": None,
            "completed_by_name": None,
            "completed_at": None,
            "source_type": None,
            "source_message_id": None,
            "source_channel_id": None,
            "source_url": None,
            "source_text": None,
            "recipient_ids": [],
            "skip_report": [],
        }

    def _default_bonus_board(self, *, mode: str, month_key: int, channel_id: int) -> Dict[str, Any]:
        now_ts = int(datetime.now(dt_timezone.utc).timestamp())
        return {
            "mode": mode,
            "month_key": month_key,
            "channel_id": channel_id,
            "message_id": None,
            "closed": False,
            "created_at": now_ts,
            "updated_at": now_ts,
            "clans": {clan_code: self._default_bonus_clan_state() for clan_code in BONUS_CLAN_ORDER},
        }

    def _normalize_bonus_board(self, board: Dict[str, Any], *, mode: str, month_key: int, channel_id: int) -> Dict[str, Any]:
        now_ts = int(datetime.now(dt_timezone.utc).timestamp())
        board.setdefault("mode", mode)
        board.setdefault("month_key", month_key)
        board.setdefault("channel_id", channel_id)
        board.setdefault("message_id", None)
        board.setdefault("closed", False)
        board.setdefault("created_at", now_ts)
        board.setdefault("updated_at", now_ts)
        board.setdefault("clans", {})
        for clan_code in BONUS_CLAN_ORDER:
            clan_state = board["clans"].get(clan_code)
            if not isinstance(clan_state, dict):
                board["clans"][clan_code] = self._default_bonus_clan_state()
            else:
                for key, value in self._default_bonus_clan_state().items():
                    clan_state.setdefault(key, value)
        return board

    def _ensure_bonus_board(
        self,
        *,
        mode: str,
        month_key: Optional[int] = None,
        channel_id: Optional[int] = None,
    ) -> tuple[str, Dict[str, Any]]:
        month_key = month_key or self._bonus_month_key()
        channel_id = channel_id or CWL_HQ_CHANNEL_ID
        board_key = self._bonus_board_key(mode, month_key)
        boards = self.bonus_dashboard_store.state.setdefault("boards", {})
        board = boards.get(board_key)
        if not isinstance(board, dict):
            board = self._default_bonus_board(mode=mode, month_key=month_key, channel_id=channel_id)
            boards[board_key] = board
        self._normalize_bonus_board(board, mode=mode, month_key=month_key, channel_id=channel_id)
        return board_key, board

    def _ensure_review_board(self, *, channel_id: int, month_key: Optional[int] = None) -> tuple[str, Dict[str, Any]]:
        month_key = month_key or self._bonus_month_key()
        board_key = self._review_board_key(month_key)
        boards = self.bonus_dashboard_store.state.setdefault("boards", {})
        board = boards.get(board_key)
        if not isinstance(board, dict):
            board = self._default_bonus_board(
                mode=BONUS_BOARD_MODE_REVIEW,
                month_key=month_key,
                channel_id=channel_id,
            )
            boards[board_key] = board
        self._normalize_bonus_board(
            board,
            mode=BONUS_BOARD_MODE_REVIEW,
            month_key=month_key,
            channel_id=int(board.get("channel_id") or channel_id),
        )
        return board_key, board

    def _get_bonus_board_by_message_id(self, message_id: int) -> tuple[Optional[str], Optional[Dict[str, Any]]]:
        boards = self.bonus_dashboard_store.state.get("boards", {})
        for board_key, board in boards.items():
            if isinstance(board, dict) and int(board.get("message_id") or 0) == message_id:
                return board_key, board
        return None, None

    def _bonus_eligibility_block_reason(self, member: discord.Member) -> Optional[str]:
        role_ids = {role.id for role in getattr(member, "roles", [])}
        if not any(role_id in MEMBERS for role_id in role_ids):
            return "not a member"
        return None

    def _bonus_public_status(self, clan_state: Dict[str, Any]) -> str:
        status = str(clan_state.get("status") or "")
        if status == BONUS_STATUS_COMPLETED:
            return "Done"
        if status == BONUS_STATUS_ON_HOLD:
            return "Hold"
        if status == BONUS_STATUS_SKIPPED:
            return "Skipped"
        return "Waiting"

    def _bonus_completed_by_name(self, clan_state: Dict[str, Any]) -> str:
        name = str(clan_state.get("completed_by_name") or "").strip()
        if name:
            return name
        status = str(clan_state.get("status") or "")
        user_id = clan_state.get("completed_by_id")
        if not isinstance(user_id, int) and status == BONUS_STATUS_SKIPPED:
            user_id = clan_state.get("last_actor_id")
        if isinstance(user_id, int):
            guild = self.bot.get_guild(GUILD_ID)
            if guild is not None:
                member = guild.get_member(user_id)
                if member is not None:
                    return member.display_name
            user = self.bot.get_user(user_id)
            if user is not None:
                return user.display_name
        return "-"

    def _bonus_board_complete(self, board: Dict[str, Any]) -> bool:
        clans = board.get("clans", {})
        return bool(clans) and all(
            isinstance(clan_state, dict)
            and clan_state.get("status") in {BONUS_STATUS_COMPLETED, BONUS_STATUS_SKIPPED}
            for clan_state in clans.values()
        )

    def _get_bonus_board_lock(self, board_key: str):
        return self.bonus_dashboard_store.lock(board_key)

    def _build_bonus_dashboard_embed(self, board: Dict[str, Any]) -> discord.Embed:
        month_key = int(board.get("month_key") or self._bonus_month_key())
        embed = discord.Embed(
            title=BONUS_BOARD_TITLE,
            description=BONUS_BOARD_DESCRIPTION,
            color=discord.Color(DEFAULT_EMBED_COLOR_HEX),
            timestamp=datetime.now(dt_timezone.utc),
        )
        embed.set_thumbnail(url=DEFAULT_THUMBNAIL_URL)
        embed.set_image(url=f"attachment://{self._bonus_dashboard_image_name(month_key)}")
        embed.set_footer(
            text=(
                "All clans are complete for this month. This board is now closed."
                if board.get("closed")
                else BONUS_BOARD_FOOTER
            )
        )
        return embed

    def _bonus_dashboard_image_name(self, month_key: int) -> str:
        return f"cwl_bonus_board_{month_key}.png"

    def _render_bonus_dashboard_image(self, board: Dict[str, Any]) -> io.BytesIO:
        month_key = int(board.get("month_key") or self._bonus_month_key())
        month_label = self._bonus_month_label(month_key)
        clans = board.get("clans", {})

        rows: List[List[str]] = []
        status_fills: List[str] = []
        for clan_code in BONUS_CLAN_ORDER:
            clan_state = clans.get(clan_code, {})
            if not isinstance(clan_state, dict):
                clan_state = self._default_bonus_clan_state()
            public_status = self._bonus_public_status(clan_state)
            if public_status == "Done":
                status_fills.append("#DCFCE7")
            elif public_status == "Hold":
                status_fills.append("#DBEAFE")
            elif public_status == "Skipped":
                status_fills.append("#E5E7EB")
            else:
                status_fills.append("#FEF3C7")
            rows.append(
                [
                    CLAN_NAMES.get(clan_code, clan_code),
                    public_status,
                    self._bonus_completed_by_name(clan_state),
                ]
            )

        row_count = max(len(rows), 1)
        fig = plt.figure(figsize=(12.8, 7.2), facecolor="#FFFFFF")
        ax = fig.add_axes([0.0, 0.0, 1.0, 1.0])
        ax.set_facecolor("#FFFFFF")
        ax.axis("off")

        ax.add_patch(plt.Rectangle((0.04, 0.86), 0.92, 0.09, transform=ax.transAxes, color="#E8F0FE", ec="none"))
        ax.text(
            0.04,
            0.905,
            "CWL Economy Rewards",
            transform=ax.transAxes,
            fontsize=22,
            fontweight="bold",
            va="center",
            ha="left",
            color="#0F172A",
        )
        ax.text(
            0.04,
            0.85,
            month_label,
            transform=ax.transAxes,
            fontsize=15,
            fontweight="semibold",
            va="top",
            ha="left",
            color="#475569",
        )
        ax.text(
            0.04,
            0.055,
            "Use this board after the bonuses are given out and the member list is posted in the clan's CWL Bonuses thread.",
            transform=ax.transAxes,
            fontsize=9.5,
            va="center",
            ha="left",
            color="#64748B",
        )

        table = ax.table(
            cellText=rows,
            colLabels=["Clan", "Status", "Handled By"],
            cellLoc="left",
            colLoc="left",
            colWidths=[0.28, 0.18, 0.54],
            bbox=[0.04, 0.11, 0.92, 0.66],
        )
        table.auto_set_font_size(False)
        table.set_fontsize(12)

        for col_index in range(3):
            cell = table[(0, col_index)]
            cell.set_facecolor("#F8FAFC")
            cell.set_edgecolor("#CBD5E1")
            cell.set_linewidth(1.0)
            cell.get_text().set_fontweight("bold")
            cell.get_text().set_color("#0F172A")

        for row_index in range(1, row_count + 1):
            clan_code = BONUS_CLAN_ORDER[row_index - 1]
            palette = BONUS_CLAN_COLORS.get(clan_code, {"accent": "#475569", "soft": "#F8FAFC"})
            base_color = "#FFFFFF"
            for col_index in range(3):
                cell = table[(row_index, col_index)]
                cell.set_edgecolor("#E2E8F0")
                cell.set_linewidth(0.8)
                cell.set_facecolor(base_color)
                cell.get_text().set_color("#0F172A")

            clan_cell = table[(row_index, 0)]
            clan_cell.set_facecolor(palette["soft"])
            clan_cell.get_text().set_fontweight("bold")
            clan_cell.get_text().set_color(palette["accent"])

            status_cell = table[(row_index, 1)]
            status_cell.set_facecolor(status_fills[row_index - 1])
            status_cell.get_text().set_fontweight("bold")
            status_value = rows[row_index - 1][1]
            if status_value == "Done":
                status_cell.get_text().set_color("#166534")
            elif status_value == "Hold":
                status_cell.get_text().set_color("#1D4ED8")
            elif status_value == "Skipped":
                status_cell.get_text().set_color("#374151")
            else:
                status_cell.get_text().set_color("#92400E")

            done_by_cell = table[(row_index, 2)]
            done_by_cell.get_text().set_color("#334155")

        image = io.BytesIO()
        fig.savefig(image, format="png", dpi=200, facecolor=fig.get_facecolor())
        plt.close(fig)
        image.seek(0)
        return image

    def _build_bonus_dashboard_file(self, board: Dict[str, Any]) -> discord.File:
        month_key = int(board.get("month_key") or self._bonus_month_key())
        return discord.File(
            fp=self._render_bonus_dashboard_image(board),
            filename=self._bonus_dashboard_image_name(month_key),
        )

    def _bonus_month_pattern(self, month_label: str) -> re.Pattern[str]:
        return re.compile(rf"\b{re.escape(month_label)}\b", re.IGNORECASE)

    def _extract_bonus_recipient_ids(self, content: str) -> List[int]:
        content = html.unescape(str(content or ""))
        ids: List[int] = []
        seen: set[int] = set()

        def append_id(raw_user_id: str) -> None:
            user_id = int(raw_user_id)
            if user_id in seen:
                return
            seen.add(user_id)
            ids.append(user_id)

        for match in re.finditer(r"<@!?(\d+)>", content):
            append_id(match.group(1))

        if ids:
            return ids

        # Last-resort paste fallback: accept one raw Discord user ID per line.
        # This covers copied mention lists that lost their <@...> wrappers without
        # accidentally treating role mentions or custom emoji IDs as recipients.
        for line in content.splitlines():
            stripped = line.strip()
            if not stripped or "<@&" in stripped or "@&" in stripped or "<:" in stripped or "<#" in stripped:
                continue
            candidate = re.sub("^[\\s>*`~_\\-\\u2022]+|[\\s<@!>*`~_.,;:]+$", "", stripped)
            if not re.fullmatch(r"\d{15,22}", candidate):
                continue
            append_id(candidate)

        return ids

    def _message_bonus_recipient_ids(self, message: discord.Message) -> List[int]:
        recipient_ids: List[int] = []
        seen: set[int] = set()
        for user in getattr(message, "mentions", None) or []:
            user_id = getattr(user, "id", None)
            if not isinstance(user_id, int) or user_id in seen:
                continue
            seen.add(user_id)
            recipient_ids.append(user_id)
        if recipient_ids:
            return recipient_ids
        return self._extract_bonus_recipient_ids(message.content or "")

    def _bonus_text_missing_month_detail(self, month_label: str, text: str) -> str:
        month_match = re.search(
            r"\b("
            r"January|February|March|April|May|June|July|August|September|October|November|December"
            r")\s+\d{4}\b",
            text or "",
            re.IGNORECASE,
        )
        if month_match:
            found_label = month_match.group(0)
            return f"That text says **{found_label}**, but this board is for **{month_label}**."
        return f"I couldn't find the **{month_label}** month header in that text."

    def _bonus_text_parse_failure_detail(
        self,
        text: str,
        *,
        month_key: int,
        require_month: bool,
    ) -> str:
        month_label = self._bonus_month_label(month_key)
        normalized_text = html.unescape(str(text or ""))
        if not normalized_text.strip():
            return "Paste the full bonus message and try again."
        if require_month and not self._bonus_month_pattern(month_label).search(normalized_text):
            return self._bonus_text_missing_month_detail(month_label, normalized_text)
        if not self._extract_bonus_recipient_ids(normalized_text):
            role_count = len(re.findall(r"<@&\d+>", normalized_text))
            channel_count = len(re.findall(r"<#\d+>", normalized_text))
            emoji_count = len(re.findall(r"<a?:[^:\s>]+:\d+>", normalized_text))
            raw_id_count = len(re.findall(r"\b\d{15,22}\b", normalized_text))
            if role_count and not raw_id_count:
                return (
                    "I found role mentions, but no member mentions. Paste the full "
                    "bonus post with the members included."
                )
            if role_count:
                return (
                    "I found role mentions, but no usable member mentions. "
                    "Paste the full bonus post, or put one member ID on each line."
                )
            if channel_count or emoji_count:
                found_parts = []
                if channel_count:
                    found_parts.append("channel mentions")
                if emoji_count:
                    found_parts.append("custom emoji")
                found = " and ".join(found_parts)
                return (
                    f"I found {found}, but no member mentions. Paste the full bonus "
                    "post with the members included."
                )
            if re.search(r"(?m)(^|\s)@", normalized_text):
                return (
                    "I found @ text, but no usable member mentions. Paste the original "
                    "post again, or put one member ID on each line."
                )
            return (
                "I couldn't find any member mentions in that text. "
                "Paste the full bonus post, or put one member ID on each line."
            )
        return "I couldn't read that bonus message. Paste the full post and try again."

    async def _resolve_bonus_member(self, guild: discord.Guild, user_id: int) -> Optional[discord.Member]:
        member = guild.get_member(user_id)
        if member is not None:
            return member
        try:
            fetched = await guild.fetch_member(user_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return None
        return fetched if isinstance(fetched, discord.Member) else None

    async def _resolve_bonus_thread(self, clan_code: str) -> Optional[discord.Thread]:
        thread_id = BONUS_THREADS.get(clan_code)
        if not thread_id:
            return None
        thread = self.bot.get_channel(thread_id)
        if thread is None:
            try:
                thread = await self.bot.fetch_channel(thread_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return None
        if isinstance(thread, discord.Thread):
            return thread
        return None

    def _build_bonus_candidate_from_message(
        self,
        clan_code: str,
        message: discord.Message,
        *,
        month_key: Optional[int] = None,
        require_month: bool = True,
        source_type: str = "scan",
    ) -> Optional[BonusCandidate]:
        month_key = month_key or self._bonus_month_key()
        month_label = self._bonus_month_label(month_key)
        content = message.content or ""
        if not content:
            return None
        if require_month and not self._bonus_month_pattern(month_label).search(content):
            return None
        recipient_ids = self._message_bonus_recipient_ids(message)
        if not recipient_ids:
            return None
        return BonusCandidate(
            month_key=month_key,
            month_label=month_label,
            clan_code=clan_code,
            source_type=source_type,
            source_message_id=message.id,
            source_channel_id=message.channel.id,
            source_url=message.jump_url,
            source_author_id=message.author.id,
            source_author_name=getattr(message.author, "display_name", message.author.name),
            source_created_at=int(message.created_at.timestamp()),
            source_text=content,
            recipient_ids=recipient_ids,
        )

    def _build_bonus_candidate_from_text(
        self,
        clan_code: str,
        text: str,
        *,
        month_key: Optional[int] = None,
        require_month: bool = True,
    ) -> Optional[BonusCandidate]:
        month_key = month_key or self._bonus_month_key()
        month_label = self._bonus_month_label(month_key)
        if not text:
            return None
        if require_month and not self._bonus_month_pattern(month_label).search(text):
            return None
        recipient_ids = self._extract_bonus_recipient_ids(text)
        if not recipient_ids:
            return None
        return BonusCandidate(
            month_key=month_key,
            month_label=month_label,
            clan_code=clan_code,
            source_type="paste",
            source_message_id=None,
            source_channel_id=None,
            source_url=None,
            source_author_id=None,
            source_author_name=None,
            source_created_at=None,
            source_text=text,
            recipient_ids=recipient_ids,
        )

    async def _scan_bonus_thread(self, clan_code: str, *, month_key: Optional[int] = None) -> tuple[Optional[BonusCandidate], str]:
        if not CWL_BONUS_ECONOMY_ENABLED:
            return None, "CWL economy rewards are temporarily disabled."
        thread = await self._resolve_bonus_thread(clan_code)
        month_key = month_key or self._bonus_month_key()
        month_label = self._bonus_month_label(month_key)
        if thread is None:
            return None, f"I couldn't read {clan_code}'s bonus thread. Ask leadership to check the thread mapping or permissions."
        async for message in thread.history(limit=200, oldest_first=False):
            if getattr(message.author, "bot", False):
                continue
            candidate = self._build_bonus_candidate_from_message(clan_code, message, month_key=month_key)
            if candidate is not None:
                return candidate, ""
        return None, f"I couldn't find one clear bonus post for {month_label} in {clan_code}'s CWL Bonuses thread."

    def _bonus_preview_embed(self, candidate: BonusCandidate) -> discord.Embed:
        if candidate.clan_code in {"BEH", "BE4"}:
            rewards_value = "The members mentioned below will receive the rewards:\nRaffle ticket: 1"
        else:
            rewards_value = (
                "The members mentioned below will receive the rewards:\n"
                "Elder: 10 coins\nMember: 5 coins"
            )
        preview_text = candidate.source_text.strip()
        if len(preview_text) > 900:
            preview_text = preview_text[:897].rstrip() + "..."
        embed = discord.Embed(
            title="Confirm CWL Bonus Post",
            color=discord.Color(DEFAULT_EMBED_COLOR_HEX),
            timestamp=datetime.now(dt_timezone.utc),
        )
        embed.add_field(name="Rewards", value=rewards_value, inline=False)
        embed.add_field(
            name="Members",
            value="\n".join(f"- <@{user_id}>" for user_id in candidate.recipient_ids),
            inline=False,
        )
        embed.add_field(name="Post preview", value=preview_text or "-", inline=False)
        if candidate.clan_code not in {"BEH", "BE4"}:
            embed.set_footer(text="Leadership is excluded from rewards.")
        return embed

    def _bonus_failure_embed(self, detail: str) -> discord.Embed:
        embed = discord.Embed(
            title="Bonus Post Couldn't Be Confirmed",
            description=detail,
            color=discord.Color.from_str("#b45e5e"),
            timestamp=datetime.now(dt_timezone.utc),
        )
        embed.add_field(
            name="Possible reasons",
            value=(
                "- The bonus post was split across several messages.\n"
                "- Later discussion made the bonus post unclear.\n"
                "- More than one message looked like the bonus post.\n"
                "- The month heading or member mentions were missing.\n"
                "- The bonus post was never sent.\n"
                "- The clan did not take part in CWL this month."
            ),
            inline=False,
        )
        embed.add_field(
            name="What to do next",
            value=(
                "Choose **Link Message** if you have the Discord link, or **Paste Text** "
                "if you have the full post. Choose **Skip Clan** if this clan should be "
                "left out for this month."
            ),
            inline=False,
        )
        return embed

    def _bonus_success_embed(
        self,
        candidate: BonusCandidate,
        summary: BonusGrantSummary,
        actor: discord.Member,
    ) -> discord.Embed:
        embed = discord.Embed(
            title="CWL Rewards Sent",
            description=f"CWL rewards for {candidate.clan_code} were sent to the members in the confirmed bonus post.",
            color=discord.Color.green(),
            timestamp=datetime.now(dt_timezone.utc),
        )
        embed.add_field(name="Sent By", value=actor.display_name, inline=True)
        embed.add_field(name="Members rewarded", value=str(summary.recipient_count), inline=True)
        embed.add_field(name="Skipped", value=str(len(summary.skipped)), inline=True)
        if summary.reward_kind == "ticket":
            embed.add_field(
                name="Tickets",
                value=self._trim_embed_value("\n".join(summary.granted) or "*None*"),
                inline=False,
            )
        else:
            embed.add_field(
                name="Elders",
                value=self._trim_embed_value("\n".join(summary.elder_granted) or "*None*"),
                inline=False,
            )
            embed.add_field(
                name="Members",
                value=self._trim_embed_value("\n".join(summary.member_granted) or "*None*"),
                inline=False,
            )
        if summary.skipped:
            embed.add_field(name="Skip details", value=self._trim_embed_value("\n".join(summary.skipped[:10])), inline=False)
        embed.set_footer(text="The bonus board has been updated.")
        return embed

    def _trim_embed_value(self, text: str, limit: int = 1024) -> str:
        text = text.strip()
        if len(text) <= limit:
            return text
        return text[: limit - 3].rstrip() + "..."

    async def _process_bonus_candidate(
        self,
        candidate: BonusCandidate,
        actor: discord.Member,
    ) -> BonusGrantSummary:
        guild = self.bot.get_guild(GUILD_ID)
        if guild is None:
            raise RuntimeError("CWL rewards can't be sent because the server couldn't be loaded.")
        reward_kind = "ticket" if candidate.clan_code in {"BEH", "BE4"} else "coins"

        prepared: List[discord.Member] = []
        skipped: List[str] = []
        for user_id in candidate.recipient_ids:
            member = await self._resolve_bonus_member(guild, user_id)
            if member is None:
                skipped.append(f"- <@{user_id}>: not in server")
                continue
            reward_exclusion = (
                self.achievement_rewards.cwl_exclusion_reason(member)
            )
            if reward_exclusion:
                skipped.append(
                    f"- <@{user_id}>: {reward_exclusion}"
                )
                continue
            blocked_reason = self._bonus_eligibility_block_reason(member)
            if blocked_reason:
                skipped.append(f"- <@{user_id}>: {blocked_reason}")
                continue
            prepared.append(member)

        result = await self.achievement_rewards.grant_cwl_rewards(
            prepared,
            reward_kind=reward_kind,
            reason=(
                f"cwl_bonus_{candidate.clan_code}_"
                f"{candidate.month_key}"
            ),
            actor_id=actor.id,
        )
        skipped.extend(
            f"- <@{user_id}>: {reason}"
            for user_id, reason in result.skipped
        )
        if not result.granted_ids:
            raise RuntimeError(
                "No eligible members were found in the bonus post."
            )
        return BonusGrantSummary(
            reward_kind=reward_kind,
            granted=[
                f"- <@{user_id}>"
                for user_id in result.granted_ids
            ],
            skipped=skipped,
            elder_granted=[
                f"- <@{user_id}>: {amount} coins"
                for user_id, amount in result.elder_grants
            ],
            member_granted=[
                f"- <@{user_id}>: {amount} coins"
                for user_id, amount in result.member_grants
            ],
            recipient_count=len(candidate.recipient_ids),
        )

    async def _set_bonus_board_ready(
        self,
        board: Dict[str, Any],
        candidate: BonusCandidate,
        actor: discord.Member,
        *,
        source_type: str,
    ) -> None:
        clan_state = board["clans"][candidate.clan_code]
        clan_state["status"] = BONUS_STATUS_READY
        clan_state["last_actor_id"] = actor.id
        clan_state["source_type"] = source_type
        clan_state["source_message_id"] = candidate.source_message_id
        clan_state["source_channel_id"] = candidate.source_channel_id
        clan_state["source_url"] = candidate.source_url
        clan_state["source_text"] = candidate.source_text
        clan_state["recipient_ids"] = list(candidate.recipient_ids)
        clan_state["completed_by_id"] = None
        clan_state["completed_by_name"] = None
        clan_state["completed_at"] = None
        clan_state["skip_report"] = []
        board["updated_at"] = int(datetime.now(dt_timezone.utc).timestamp())
        self.bonus_dashboard_store.save()

    async def _set_bonus_board_needs_review(
        self,
        board: Dict[str, Any],
        clan_code: str,
        actor: discord.Member,
    ) -> None:
        clan_state = board["clans"][clan_code]
        clan_state["status"] = BONUS_STATUS_NEEDS_REVIEW
        clan_state["last_actor_id"] = actor.id
        board["updated_at"] = int(datetime.now(dt_timezone.utc).timestamp())
        self.bonus_dashboard_store.save()

    async def _set_bonus_board_skipped(
        self,
        board: Dict[str, Any],
        clan_code: str,
        actor: discord.Member,
    ) -> None:
        clan_state = board["clans"][clan_code]
        clan_state["status"] = BONUS_STATUS_SKIPPED
        clan_state["last_actor_id"] = actor.id
        clan_state["completed_by_id"] = None
        clan_state["completed_by_name"] = None
        clan_state["completed_at"] = None
        board["updated_at"] = int(datetime.now(dt_timezone.utc).timestamp())
        self.bonus_dashboard_store.save()

    async def _set_bonus_board_on_hold(
        self,
        board: Dict[str, Any],
        clan_code: str,
        actor: discord.Member,
    ) -> None:
        clan_state = board["clans"][clan_code]
        clan_state["status"] = BONUS_STATUS_ON_HOLD
        clan_state["last_actor_id"] = actor.id
        clan_state["completed_by_id"] = None
        clan_state["completed_by_name"] = None
        clan_state["completed_at"] = None
        board["updated_at"] = int(datetime.now(dt_timezone.utc).timestamp())
        self.bonus_dashboard_store.save()

    async def _edit_bonus_board_message(
        self,
        board: Dict[str, Any],
        *,
        message: Optional[discord.Message] = None,
    ) -> Optional[discord.Message]:
        if message is None:
            message_id = board.get("message_id")
            channel_id = board.get("channel_id")
            if not isinstance(message_id, int) or not isinstance(channel_id, int):
                return None
            channel = self.bot.get_channel(channel_id)
            if channel is None:
                try:
                    channel = await self.bot.fetch_channel(channel_id)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    return None
            if channel is None:
                return None
            try:
                message = await channel.fetch_message(message_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return None
        embed = self._build_bonus_dashboard_embed(board)
        dashboard_image = self._build_bonus_dashboard_file(board)
        view = CwlBonusDashboardView(self, board_closed=bool(board.get("closed")))
        await message.edit(embed=embed, attachments=[dashboard_image], view=view)
        return message

    async def _post_bonus_dashboard(
        self,
        *,
        mode: str,
        target_channel: discord.TextChannel,
        ping_helper: bool,
    ) -> None:
        existing_message: Optional[discord.Message] = None
        month_key = self._bonus_month_key()
        if mode == BONUS_BOARD_MODE_REVIEW:
            _, board = self._ensure_review_board(channel_id=target_channel.id, month_key=month_key)
        else:
            _, board = self._ensure_bonus_board(mode=mode, month_key=month_key, channel_id=target_channel.id)

        message_id = board.get("message_id")
        message_channel_id = int(board.get("channel_id") or 0)
        if isinstance(message_id, int) and message_channel_id == target_channel.id:
            try:
                existing_message = await target_channel.fetch_message(message_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                existing_message = None

        embed = self._build_bonus_dashboard_embed(board)
        dashboard_image = self._build_bonus_dashboard_file(board)
        view = CwlBonusDashboardView(self, board_closed=bool(board.get("closed")))
        content = f"<@&{HELPER_CWL_BASE_ROLE_ID}>" if ping_helper and mode == BONUS_BOARD_MODE_FINAL else None
        if existing_message is None:
            sent = await target_channel.send(content=content, embed=embed, file=dashboard_image, view=view)
            board["message_id"] = sent.id
        else:
            await existing_message.edit(content=content, embed=embed, attachments=[dashboard_image], view=view)
            board["message_id"] = existing_message.id
        board["channel_id"] = target_channel.id
        board["updated_at"] = int(datetime.now(dt_timezone.utc).timestamp())
        self.bonus_dashboard_store.save()

    async def _mark_bonus_board_complete(self, board: Dict[str, Any]) -> None:
        if self._bonus_board_complete(board):
            board["closed"] = True
            board["updated_at"] = int(datetime.now(dt_timezone.utc).timestamp())
            self.bonus_dashboard_store.save()

    async def _handle_bonus_clan_selection(
        self,
        interaction: discord.Interaction,
        board_key: str,
        board: Dict[str, Any],
        clan_code: str,
    ) -> None:
        if board.get("closed"):
            await interaction.edit_original_response(
                content="This board is already complete for this month.",
                embed=None,
                view=BonusContinueView(self, board_key, board, board_closed=True),
            )
            return
        clan_state = board["clans"][clan_code]
        if clan_state.get("status") == BONUS_STATUS_COMPLETED:
            await interaction.edit_original_response(
                content=f"{clan_code} has already been completed for this month.",
                embed=None,
                view=BonusContinueView(self, board_key, board, board_closed=bool(board.get("closed"))),
            )
            return
        current_status = str(clan_state.get("status") or "")
        candidate, detail = await self._scan_bonus_thread(clan_code, month_key=board["month_key"])
        if candidate is None:
            if current_status not in {BONUS_STATUS_ON_HOLD, BONUS_STATUS_SKIPPED}:
                await self._set_bonus_board_needs_review(board, clan_code, interaction.user)
            await self._edit_bonus_board_message(board)
            await interaction.edit_original_response(
                content=None,
                embed=self._bonus_failure_embed(detail),
                view=BonusFallbackView(self, board_key, board, clan_code),
            )
            return
        await self._set_bonus_board_ready(board, candidate, interaction.user, source_type="scan")
        await self._edit_bonus_board_message(board)
        await interaction.edit_original_response(
            content=None,
            embed=self._bonus_preview_embed(candidate),
            view=BonusPreviewView(self, board_key, board, candidate),
        )

    async def _handle_bonus_link_submission(
        self,
        interaction: discord.Interaction,
        board_key: str,
        board: Dict[str, Any],
        clan_code: str,
        message_link: str,
    ) -> None:
        if board.get("closed"):
            await interaction.response.send_message(
                "This board is already complete for this month.",
                ephemeral=True,
                view=BonusContinueView(self, board_key, board, board_closed=True),
            )
            return
        clan_state = board["clans"][clan_code]
        if clan_state.get("status") == BONUS_STATUS_COMPLETED:
            await interaction.response.send_message(
                f"{clan_code} has already been completed for this month.",
                ephemeral=True,
                view=BonusContinueView(self, board_key, board, board_closed=bool(board.get("closed"))),
            )
            return
        await interaction.response.defer(ephemeral=True)
        candidate = await self._parse_bonus_message_link(clan_code, message_link, month_key=board["month_key"])
        if candidate is None:
            await interaction.edit_original_response(
                content="That link didn't point to a valid bonus post for this clan.",
                embed=None,
                view=BonusContinueView(self, board_key, board, board_closed=bool(board.get("closed"))),
            )
            return
        await self._set_bonus_board_ready(board, candidate, interaction.user, source_type="link")
        await self._edit_bonus_board_message(board)
        await interaction.edit_original_response(
            content=None,
            embed=self._bonus_preview_embed(candidate),
            view=BonusPreviewView(self, board_key, board, candidate),
        )

    async def _handle_bonus_text_submission(
        self,
        interaction: discord.Interaction,
        board_key: str,
        board: Dict[str, Any],
        clan_code: str,
        pasted_text: str,
    ) -> None:
        if board.get("closed"):
            await interaction.response.send_message(
                "This board is already complete for this month.",
                ephemeral=True,
                view=BonusContinueView(self, board_key, board, board_closed=True),
            )
            return
        clan_state = board["clans"][clan_code]
        if clan_state.get("status") == BONUS_STATUS_COMPLETED:
            await interaction.response.send_message(
                f"{clan_code} has already been completed for this month.",
                ephemeral=True,
                view=BonusContinueView(self, board_key, board, board_closed=bool(board.get("closed"))),
            )
            return
        await interaction.response.defer(ephemeral=True)
        candidate = self._build_bonus_candidate_from_text(
            clan_code,
            pasted_text,
            month_key=board["month_key"],
            require_month=False,
        )
        if candidate is None:
            detail = self._bonus_text_parse_failure_detail(
                pasted_text,
                month_key=int(board["month_key"]),
                require_month=False,
            )
            await interaction.edit_original_response(
                content=detail,
                embed=None,
                view=BonusContinueView(self, board_key, board, board_closed=bool(board.get("closed"))),
            )
            return
        await self._set_bonus_board_ready(board, candidate, interaction.user, source_type="paste")
        await self._edit_bonus_board_message(board)
        await interaction.edit_original_response(
            content=None,
            embed=self._bonus_preview_embed(candidate),
            view=BonusPreviewView(self, board_key, board, candidate),
        )

    async def _confirm_bonus_candidate(
        self,
        interaction: discord.Interaction,
        board_key: str,
        board: Dict[str, Any],
        candidate: BonusCandidate,
    ) -> None:
        if not CWL_BONUS_ECONOMY_ENABLED:
            await interaction.edit_original_response(
                content="CWL economy rewards are temporarily disabled.",
                embed=None,
                view=None,
            )
            return
        lock = self._get_bonus_board_lock(board_key)
        async with lock:
            if board.get("closed"):
                await interaction.edit_original_response(
                    content="This board is already complete for this month.",
                    embed=None,
                    view=BonusContinueView(self, board_key, board, board_closed=True),
                )
                return
            clan_state = board["clans"][candidate.clan_code]
            if clan_state.get("status") == BONUS_STATUS_COMPLETED:
                await interaction.edit_original_response(
                    content=f"{candidate.clan_code} has already been completed for this month.",
                    embed=None,
                    view=BonusContinueView(self, board_key, board, board_closed=bool(board.get("closed"))),
                )
                return
            try:
                summary = await self._process_bonus_candidate(candidate, interaction.user)
            except RuntimeError as e:
                clan_state["status"] = BONUS_STATUS_NEEDS_REVIEW
                clan_state["last_actor_id"] = interaction.user.id
                board["updated_at"] = int(datetime.now(dt_timezone.utc).timestamp())
                self.bonus_dashboard_store.save()
                await self._edit_bonus_board_message(board)
                await interaction.edit_original_response(
                    content=str(e),
                    embed=None,
                    view=BonusContinueView(self, board_key, board, board_closed=bool(board.get("closed"))),
                )
                return
            except Exception:
                LOGGER.exception("Failed to process CWL bonus candidate for %s", candidate.clan_code)
                clan_state["status"] = BONUS_STATUS_NEEDS_REVIEW
                clan_state["last_actor_id"] = interaction.user.id
                board["updated_at"] = int(datetime.now(dt_timezone.utc).timestamp())
                self.bonus_dashboard_store.save()
                await self._edit_bonus_board_message(board)
                await interaction.edit_original_response(
                    content="The rewards couldn't be sent, so the clan was left for review.",
                    embed=None,
                    view=BonusContinueView(self, board_key, board, board_closed=bool(board.get("closed"))),
                )
                return
            clan_state = board["clans"][candidate.clan_code]
            clan_state["status"] = BONUS_STATUS_COMPLETED
            clan_state["completed_by_id"] = interaction.user.id
            clan_state["completed_by_name"] = interaction.user.display_name
            clan_state["completed_at"] = int(datetime.now(dt_timezone.utc).timestamp())
            clan_state["last_actor_id"] = interaction.user.id
            clan_state["source_type"] = candidate.source_type
            clan_state["source_message_id"] = candidate.source_message_id
            clan_state["source_channel_id"] = candidate.source_channel_id
            clan_state["source_url"] = candidate.source_url
            clan_state["source_text"] = candidate.source_text
            clan_state["recipient_ids"] = list(candidate.recipient_ids)
            clan_state["skip_report"] = list(summary.skipped)
            board["updated_at"] = int(datetime.now(dt_timezone.utc).timestamp())
            self.bonus_dashboard_store.save()
            await self._mark_bonus_board_complete(board)
            await self._edit_bonus_board_message(board)
            await interaction.edit_original_response(
                content=None,
                embed=self._bonus_success_embed(candidate, summary, interaction.user),
                view=BonusContinueView(self, board_key, board, board_closed=bool(board.get("closed"))),
            )

    async def _parse_bonus_message_link(
        self,
        clan_code: str,
        message_link: str,
        *,
        month_key: Optional[int] = None,
    ) -> Optional[BonusCandidate]:
        match = re.search(r"discord(?:app)?\.com/channels/(\d+)/(\d+)/(\d+)", message_link)
        if match is None:
            return None
        channel_id = int(match.group(2))
        message_id = int(match.group(3))
        expected_thread_id = BONUS_THREADS.get(clan_code)
        if expected_thread_id is None or channel_id != expected_thread_id:
            return None
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return None
        if not isinstance(channel, discord.Thread):
            return None
        try:
            message = await channel.fetch_message(message_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return None
        return self._build_bonus_candidate_from_message(
            clan_code,
            message,
            month_key=month_key,
            require_month=False,
            source_type="link",
        )


class CwlBonusDashboardView(discord.ui.View):
    def __init__(self, cog: CwlBonusDashboardMixin, *, board_closed: bool = False):
        super().__init__(timeout=None)
        self.cog = cog
        self.board_closed = board_closed

        handle_button = discord.ui.Button(
            label="Handle Clan",
            style=discord.ButtonStyle.primary,
            custom_id="cwl_bonus_dashboard:handle",
        )
        handle_button.disabled = board_closed
        handle_button.callback = self.handle_clan
        self.add_item(handle_button)

    async def handle_clan(self, interaction: discord.Interaction) -> None:
        if not CWL_BONUS_ECONOMY_ENABLED:
            await interaction.response.send_message(
                "CWL economy rewards are temporarily disabled.",
                ephemeral=True,
            )
            return
        board_key, board = self.cog._get_bonus_board_by_message_id(interaction.message.id)
        if board_key is None or board is None:
            await interaction.response.send_message("This message is no longer linked to a clan on the CWL bonus board.", ephemeral=True)
            return
        if board.get("closed"):
            await interaction.response.send_message(
                "This board is already complete for this month.",
                ephemeral=True,
                view=BonusContinueView(self.cog, board_key, board, board_closed=True),
            )
            return
        await interaction.response.send_message(view=BonusClanSelectView(self.cog, board_key, board), ephemeral=True)

class BonusClanSelectView(discord.ui.View):
    def __init__(self, cog: CwlBonusDashboardMixin, board_key: str, board: Dict[str, Any]):
        super().__init__(timeout=600)
        self.cog = cog
        self.board_key = board_key
        self.board = board
        self.select_menu = discord.ui.Select(
            placeholder="Choose your clan",
            min_values=1,
            max_values=1,
            options=[discord.SelectOption(label=CLAN_NAMES[code], value=code) for code in BONUS_CLAN_ORDER],
        )
        self.select_menu.callback = self.select_clan
        self.add_item(self.select_menu)

    async def select_clan(self, interaction: discord.Interaction) -> None:
        clan_code = str(self.select_menu.values[0])
        await interaction.response.edit_message(content="Checking the thread...", view=None)
        await self.cog._handle_bonus_clan_selection(interaction, self.board_key, self.board, clan_code)


class BonusPreviewView(discord.ui.View):
    def __init__(self, cog: CwlBonusDashboardMixin, board_key: str, board: Dict[str, Any], candidate: BonusCandidate):
        super().__init__(timeout=600)
        self.cog = cog
        self.board_key = board_key
        self.board = board
        self.candidate = candidate

        confirm_button = discord.ui.Button(label="Looks right", style=discord.ButtonStyle.success)
        confirm_button.callback = self.confirm
        self.add_item(confirm_button)

        fallback_button = discord.ui.Button(label="Looks off", style=discord.ButtonStyle.secondary)
        fallback_button.callback = self.use_fallback
        self.add_item(fallback_button)

        cancel_button = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary)
        cancel_button.callback = self.cancel
        self.add_item(cancel_button)

    async def confirm(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        await interaction.edit_original_response(content="Sending rewards...", embed=None, view=None)
        await self.cog._confirm_bonus_candidate(interaction, self.board_key, self.board, self.candidate)

    async def use_fallback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        await interaction.edit_original_response(
            embed=self.cog._bonus_failure_embed(
                "I couldn't identify one clear bonus post. Link the message or paste its full text instead.",
            ),
            view=BonusFallbackView(self.cog, self.board_key, self.board, self.candidate.clan_code, candidate=self.candidate),
        )

    async def cancel(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        await interaction.edit_original_response(
            content="No rewards were sent.",
            embed=None,
            view=BonusContinueView(self.cog, self.board_key, self.board),
        )


class BonusContinueView(discord.ui.View):
    def __init__(self, cog: CwlBonusDashboardMixin, board_key: str, board: Dict[str, Any], *, board_closed: bool = False):
        super().__init__(timeout=600)
        self.cog = cog
        self.board_key = board_key
        self.board = board
        self.board_closed = board_closed

        choose_button = discord.ui.Button(label="Choose Another Clan", style=discord.ButtonStyle.primary)
        choose_button.callback = self.choose_another_clan
        choose_button.disabled = bool(board.get("closed"))
        self.add_item(choose_button)

        dismiss_button = discord.ui.Button(label="Done", style=discord.ButtonStyle.secondary)
        dismiss_button.callback = self.dismiss
        self.add_item(dismiss_button)

    async def choose_another_clan(self, interaction: discord.Interaction) -> None:
        if self.board.get("closed"):
            await interaction.response.edit_message(
                content="This board is already complete for this month.",
                embed=None,
                view=BonusContinueView(self.cog, self.board_key, self.board, board_closed=True),
            )
            return
        await interaction.response.edit_message(
            content="",
            embed=None,
            view=BonusClanSelectView(self.cog, self.board_key, self.board),
        )

    async def dismiss(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(content="Done.", embed=None, view=None)


class BonusLinkModal(discord.ui.Modal, title="Link Bonus Message"):
    def __init__(self, cog: CwlBonusDashboardMixin, board_key: str, board: Dict[str, Any], clan_code: str):
        super().__init__()
        self.cog = cog
        self.board_key = board_key
        self.board = board
        self.clan_code = clan_code
        self.link_input = discord.ui.TextInput(
            label="Discord message link",
            placeholder="Paste the exact bonus message link from the clan's bonus thread",
            required=True,
            max_length=300,
        )
        self.add_item(self.link_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog._handle_bonus_link_submission(
            interaction,
            self.board_key,
            self.board,
            self.clan_code,
            str(self.link_input.value),
        )


class BonusPasteModal(discord.ui.Modal, title="Paste Bonus Text"):
    def __init__(self, cog: CwlBonusDashboardMixin, board_key: str, board: Dict[str, Any], clan_code: str):
        super().__init__()
        self.cog = cog
        self.board_key = board_key
        self.board = board
        self.clan_code = clan_code
        self.text_input = discord.ui.TextInput(
            label="Bonus message text",
            placeholder="Paste the full CWL bonus message, including the month heading and member mentions",
            required=True,
            style=discord.TextStyle.paragraph,
            max_length=4000,
        )
        self.add_item(self.text_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog._handle_bonus_text_submission(
            interaction,
            self.board_key,
            self.board,
            self.clan_code,
            str(self.text_input.value),
        )


class BonusFallbackView(discord.ui.View):
    def __init__(
        self,
        cog: CwlBonusDashboardMixin,
        board_key: str,
        board: Dict[str, Any],
        clan_code: str,
        *,
        candidate: Optional[BonusCandidate] = None,
    ):
        super().__init__(timeout=600)
        self.cog = cog
        self.board_key = board_key
        self.board = board
        self.clan_code = clan_code
        self.candidate = candidate

        back_button = discord.ui.Button(label="Back", style=discord.ButtonStyle.secondary)
        back_button.callback = self.back
        self.add_item(back_button)

        link_button = discord.ui.Button(label="Link Message", style=discord.ButtonStyle.primary)
        link_button.callback = self.link_message
        self.add_item(link_button)

        paste_button = discord.ui.Button(label="Paste Text", style=discord.ButtonStyle.secondary)
        paste_button.callback = self.paste_text
        self.add_item(paste_button)

        hold_button = discord.ui.Button(label="Review Later", style=discord.ButtonStyle.secondary)
        hold_button.callback = self.hold_clan
        self.add_item(hold_button)

        skip_button = discord.ui.Button(label="Skip Clan", style=discord.ButtonStyle.danger)
        skip_button.callback = self.skip_clan
        self.add_item(skip_button)

        cancel_button = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary)
        cancel_button.callback = self.cancel
        self.add_item(cancel_button)

    async def link_message(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(BonusLinkModal(self.cog, self.board_key, self.board, self.clan_code))

    async def paste_text(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(BonusPasteModal(self.cog, self.board_key, self.board, self.clan_code))

    async def back(self, interaction: discord.Interaction) -> None:
        if self.candidate is not None:
            await interaction.response.edit_message(
                embed=self.cog._bonus_preview_embed(self.candidate),
                view=BonusPreviewView(self.cog, self.board_key, self.board, self.candidate),
            )
            return
        await interaction.response.edit_message(
            content="",
            embed=None,
            view=BonusClanSelectView(self.cog, self.board_key, self.board),
        )

    async def skip_clan(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        await self.cog._set_bonus_board_skipped(self.board, self.clan_code, interaction.user)
        await self.cog._edit_bonus_board_message(self.board)
        await interaction.edit_original_response(
            content=f"Skipped {self.clan_code} for this month.",
            embed=None,
            view=BonusContinueView(self.cog, self.board_key, self.board),
        )

    async def hold_clan(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        await self.cog._set_bonus_board_on_hold(self.board, self.clan_code, interaction.user)
        await self.cog._edit_bonus_board_message(self.board)
        await interaction.edit_original_response(
            content=f"{self.clan_code} put on hold for now.",
            embed=None,
            view=BonusContinueView(self.cog, self.board_key, self.board),
        )

    async def cancel(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        await interaction.edit_original_response(
            content="No rewards were sent.",
            embed=None,
            view=BonusContinueView(self.cog, self.board_key, self.board),
        )
