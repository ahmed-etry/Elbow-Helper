"""CWL prep and stars dashboard rendering and refresh workflows."""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import time
import textwrap
import warnings as py_warnings
from datetime import datetime
from datetime import timedelta
from datetime import timezone as dt_timezone
from typing import Any
from typing import Dict
from typing import List
from typing import Optional
from typing import Set
from typing import Tuple

import discord
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from discord.ext import tasks

from elbow_helper.domain.player_tags import encode_clash_tag
from elbow_helper.configuration.clans import CLAN_NAMES
from elbow_helper.configuration.style import DEFAULT_EMBED_COLOR_HEX
from elbow_helper.configuration.style import DEFAULT_THUMBNAIL_URL
from .helpers import coc_time_to_dt
from .helpers import wait_for_boot_complete
from elbow_helper.infrastructure.persistence import read_json
from elbow_helper.infrastructure.persistence import write_json_atomic
from .config import CWL_CLAN_TAGS
from .config import DASHBOARD_REFRESH_BACKOFF_SECONDS
from .config import DASHBOARD_REFRESH_RETRIES
from .config import DASHBOARD_STATE_FILE
from .config import DASHBOARD_THREADS
from .config import DASHBOARD_WARNING_COOLDOWN_SECONDS
from .config import HERO_SUM_CACHE_SECONDS
from .config import LEAGUE_NAME_CACHE_SECONDS
from .views import CwlPrepRefreshView


LOGGER = logging.getLogger(__name__)
timezone = dt_timezone


class CwlDashboardMixin:
    def _get_manual_dashboard_refresh_lock(self, clan_code: str) -> asyncio.Lock:
        lock = self._manual_dashboard_refresh_locks.get(clan_code)
        if lock is None:
            lock = asyncio.Lock()
            self._manual_dashboard_refresh_locks[clan_code] = lock
        return lock


    @staticmethod
    def _is_transient_dashboard_error(error: Exception) -> bool:
        if isinstance(
            error,
            (
                asyncio.TimeoutError,
                OSError,
            ),
        ):
            return True
        if isinstance(error, discord.HTTPException):
            status = getattr(error, "status", None)
            if status in {429, 500, 502, 503, 504}:
                return True
        text = str(error).lower()
        transient_markers = (
            "temporary failure in name resolution",
            "name or service not known",
            "cannot connect to host",
            "server disconnected",
            "connection reset",
            "timed out",
            "timeout",
        )
        return any(marker in text for marker in transient_markers)


    def _log_dashboard_refresh_failure(
        self,
        clan_code: str,
        context: str,
        detail: str,
        *,
        transient: bool,
    ) -> None:
        key = f"{context}:{clan_code}"
        now = time.monotonic()
        state = self._dashboard_refresh_warning_state.get(key)
        if state:
            last_detail = str(state.get("detail") or "")
            last_ts = float(state.get("last_ts") or 0.0)
            if detail == last_detail and (now - last_ts) < DASHBOARD_WARNING_COOLDOWN_SECONDS:
                state["suppressed"] = int(state.get("suppressed") or 0) + 1
                self._dashboard_refresh_warning_state[key] = state
                return

        suppressed = int((state or {}).get("suppressed") or 0)
        suffix = f" (suppressed {suppressed} similar warnings)" if suppressed else ""
        log = LOGGER.info if transient else LOGGER.warning
        if context == "startup":
            log("initial dashboard sync failed for %s: %s%s", clan_code, detail, suffix)
        elif context == "manual":
            log("prep preview refresh failed for %s: %s%s", clan_code, detail, suffix)
        else:
            log("dashboard refresh failed for %s: %s%s", clan_code, detail, suffix)
        self._dashboard_refresh_warning_state[key] = {"detail": detail, "last_ts": now, "suppressed": 0}


    async def _refresh_dashboard_with_retry(self, clan_code: str, context: str) -> bool:
        for attempt in range(1, DASHBOARD_REFRESH_RETRIES + 1):
            try:
                await self._upsert_dashboard_for_clan(clan_code)
                self._dashboard_refresh_warning_state.pop(f"{context}:{clan_code}", None)
                return True
            except (
                asyncio.TimeoutError,
                discord.HTTPException,
                RuntimeError,
                ValueError,
                OSError,
            ) as e:
                transient = self._is_transient_dashboard_error(e)
                if transient and attempt < DASHBOARD_REFRESH_RETRIES:
                    await asyncio.sleep(DASHBOARD_REFRESH_BACKOFF_SECONDS * attempt)
                    continue
                detail = str(e).strip() or e.__class__.__name__
                if attempt > 1:
                    detail = f"{detail} (after {attempt} attempts)"
                self._log_dashboard_refresh_failure(clan_code, context, detail, transient=transient)
                return False
        return False


    def _load_dashboard_state(self) -> Dict[str, Any]:
        try:
            if os.path.exists(DASHBOARD_STATE_FILE):
                data = read_json(DASHBOARD_STATE_FILE)
            else:
                data = {}
        except (OSError, json.JSONDecodeError, TypeError) as e:
            LOGGER.warning("Failed to load dashboard state: %s", e)
            data = {}
        if not isinstance(data, dict):
            data = {}
        prep_message_ids = data.get("prep_message_ids")
        if not isinstance(prep_message_ids, dict):
            prep_message_ids = {}
        stars_message_ids = data.get("stars_message_ids")
        if not isinstance(stars_message_ids, dict):
            stars_message_ids = {}
        data["prep_message_ids"] = prep_message_ids
        data["stars_message_ids"] = stars_message_ids
        return data


    def _save_dashboard_state(self) -> None:
        try:
            write_json_atomic(DASHBOARD_STATE_FILE, self.dashboard_state, indent=2)
        except (OSError, TypeError) as e:
            LOGGER.warning("Failed to save dashboard state: %s", e)


    @staticmethod
    def _message_has_dashboard_embed(message: discord.Message, title_fragment: str) -> bool:
        for embed in message.embeds:
            title = str(getattr(embed, "title", "") or "")
            if title_fragment in title:
                return True
        return False


    async def _find_dashboard_messages(
        self,
        thread: discord.Thread,
        title_fragment: str,
        *,
        limit: int = 500,
    ) -> List[discord.Message]:
        bot_user_id = getattr(getattr(self.bot, "user", None), "id", None)
        if not isinstance(bot_user_id, int):
            return []

        matches: List[discord.Message] = []
        async for message in thread.history(limit=limit):
            if getattr(message.author, "id", None) != bot_user_id:
                continue
            if self._message_has_dashboard_embed(message, title_fragment):
                matches.append(message)
        return matches


    def _war_blocks_for_clan(
        self, war: Dict[str, Any], clan_tag: str
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        own = war.get("clan", {}) or {}
        opp = war.get("opponent", {}) or {}
        if own.get("tag") == clan_tag:
            return own, opp
        if opp.get("tag") == clan_tag:
            return opp, own
        return own, opp


    async def _fetch_hero_sums(
        self,
        tags: List[str],
    ) -> Dict[str, Optional[int]]:
        results: Dict[str, Optional[int]] = {}
        sem = asyncio.Semaphore(5)
        now_ts = int(time.time())
        missing_tags: Set[str] = set()

        for tag in set(tags):
            cached = self._hero_sum_cache.get(tag)
            if (
                isinstance(cached, dict)
                and isinstance(cached.get("fetched_at"), int)
                and (now_ts - int(cached["fetched_at"])) <= HERO_SUM_CACHE_SECONDS
            ):
                results[tag] = cached.get("hero_sum")
            else:
                missing_tags.add(tag)

        async def load(tag: str) -> None:
            path = f"/players/{encode_clash_tag(tag)}"
            async with sem:
                payload = await self.bonus_analysis.fetch_json(
                    path,
                    retries=3,
                )
            if not payload:
                results[tag] = None
                self._hero_sum_cache[tag] = {"hero_sum": None, "fetched_at": now_ts}
                return
            heroes = payload.get("heroes", []) or []
            hero_sum = 0
            for hero in heroes:
                if hero.get("village") and hero.get("village") != "home":
                    continue
                level = hero.get("level")
                if isinstance(level, int):
                    hero_sum += level
            hero_value = hero_sum if hero_sum > 0 else None
            results[tag] = hero_value
            self._hero_sum_cache[tag] = {"hero_sum": hero_value, "fetched_at": now_ts}

        if missing_tags:
            await asyncio.gather(*(load(tag) for tag in missing_tags))
        return results


    async def _fetch_clan_war_league_name(
        self,
        clan_tag: str,
    ) -> Optional[str]:
        # Resolve league from the clan profile endpoint and cache it.
        now_ts = int(time.time())
        cached = self._clan_league_cache.get(clan_tag)
        if (
            isinstance(cached, dict)
            and isinstance(cached.get("fetched_at"), int)
            and (now_ts - int(cached["fetched_at"])) <= LEAGUE_NAME_CACHE_SECONDS
        ):
            cached_name = cached.get("league_name")
            return str(cached_name) if cached_name else None

        path = f"/clans/{encode_clash_tag(clan_tag)}"
        payload = await self.bonus_analysis.fetch_json(path, retries=3)
        league_name: Optional[str] = None
        if payload:
            raw_name = ((payload.get("warLeague") or {}).get("name") or "").strip()
            if raw_name:
                league_name = raw_name
        self._clan_league_cache[clan_tag] = {
            "league_name": league_name,
            "fetched_at": now_ts,
        }
        return league_name


    def _render_prep_matchup_image(
        self,
        own_name: str,
        opp_name: str,
        subtitle: str,
        rows: List[List[str]],
        status_text: Optional[str] = None,
        status_color: str = "#f0c674",
    ) -> discord.File:
        # Render a dark-themed matchup card so table alignment stays consistent in Discord.
        has_rows = bool(rows)
        fig_height = max(3.2, 1.9 + 0.34 * max(len(rows), 1))
        if not has_rows:
            fig_height = max(fig_height, 3.6)
        fig, ax = plt.subplots(figsize=(10.6, fig_height), dpi=150)
        fig.patch.set_facecolor("#232b38")
        ax.set_facecolor("#232b38")
        ax.axis("off")

        show_subtitle = has_rows or (subtitle or "").strip().lower() != "preparation preview"
        title_y = 0.965
        subtitle_y = 0.885
        status_y = 0.825 if show_subtitle else 0.885
        status_display = textwrap.fill(status_text, width=92 if has_rows else 72) if status_text else ""
        status_line_count = len(status_display.splitlines()) if status_display else 0

        ax.text(
            0.01, title_y, f"{own_name} vs {opp_name}" if has_rows else f"{own_name} - Preparation Preview",
            transform=ax.transAxes, fontsize=14, fontweight="bold", color="#e6edf3", va="top"
        )
        if show_subtitle:
            ax.text(
                0.01, subtitle_y, subtitle,
                transform=ax.transAxes, fontsize=10.5, color="#8fbcbb", va="top"
            )
        if status_text:
            ax.text(
                0.01, status_y, status_display,
                transform=ax.transAxes, fontsize=10.5, color=status_color, va="top"
            )

        if has_rows:
            own_label = self._prep_header_label(own_name)
            opp_label = self._prep_header_label(opp_name)
            col_labels = [
                "Position",
                f"{own_label} TH",
                f"{own_label} Heroes",
                f"{opp_label} TH",
                f"{opp_label} Heroes",
            ]
            # Reserve footer space for legend text and keep a clear gap under the matchup line.
            table_bottom = 0.10
            if status_text:
                table_top = max(0.69, 0.75 - 0.03 * max(status_line_count - 1, 0))
            else:
                table_top = 0.85
            table_bbox = [0.01, table_bottom, 0.98, table_top - table_bottom]
            table = ax.table(
                cellText=rows,
                colLabels=col_labels,
                cellLoc="center",
                colLoc="center",
                colWidths=[0.10, 0.22, 0.16, 0.22, 0.16],
                bbox=table_bbox,
            )
            table.auto_set_font_size(False)
            table.set_fontsize(10)
            table.scale(1, 1.2)

            for (row, _), cell in table.get_celld().items():
                cell.set_edgecolor("#3a455a")
                if row == 0:
                    cell.set_facecolor("#334155")
                    cell.get_text().set_color("#e6edf3")
                    cell.get_text().set_weight("bold")
                else:
                    cell.set_facecolor("#2a3445" if row % 2 else "#283142")
                    cell.get_text().set_color("#d8dee9")

            ax.text(
                0.01, 0.03, "TH = Town Hall  |  H = Heroes",
                transform=ax.transAxes, fontsize=9, color="#9aa7b8", va="bottom"
            )
        else:
            empty_text = status_text or "No CWL preparation day is active right now."
            ax.text(
                0.5,
                0.46,
                textwrap.fill(empty_text, width=66),
                transform=ax.transAxes,
                fontsize=12,
                color="#d8dee9",
                ha="center",
                va="center",
                bbox={"boxstyle": "round,pad=0.45", "facecolor": "#2a3445", "edgecolor": "#3a455a"},
            )
            ax.text(
                0.01, 0.03, "This preview updates when preparation day begins.",
                transform=ax.transAxes, fontsize=9, color="#9aa7b8", va="bottom"
            )

        image = io.BytesIO()
        # Keep full canvas for empty-state cards; tight cropping makes header lines look cramped.
        self._save_matplotlib_image(fig, image, tight=has_rows)
        plt.close(fig)
        image.seek(0)
        return discord.File(image, filename="cwl_prep_matchup.png")


    @staticmethod
    def _save_matplotlib_image(fig: Any, image: io.BytesIO, *, tight: bool = True) -> None:
        # Ignore known glyph warnings for non-Latin names on hosts that only have DejaVu Sans.
        with py_warnings.catch_warnings():
            py_warnings.filterwarnings(
                "ignore",
                message=r"Glyph .* missing from font\(s\) DejaVu Sans.*",
                category=UserWarning,
            )
            fig.savefig(
                image,
                format="png",
                dpi=150,
                facecolor=fig.get_facecolor(),
                bbox_inches="tight" if tight else None,
            )


    @staticmethod
    def _prep_header_label(name: str) -> str:
        # Use BE acronyms for known Brown Elbow clans; fallback to compact display names.
        for code, clan_name in CLAN_NAMES.items():
            if clan_name == name:
                return code
        return name[:10]


    @staticmethod
    def _cwl_guaranteed_bonus_slots(league_name: Optional[str]) -> int:
        # In-game CWL bonus slots start with a league-based guaranteed amount.
        if not league_name:
            return 0
        normalized = league_name.lower()
        if "champion" in normalized:
            return 4
        if "master" in normalized:
            return 3
        if "crystal" in normalized or "gold" in normalized:
            return 2
        if "silver" in normalized or "bronze" in normalized:
            return 1
        return 0


    def _did_clan_win_war(self, war: Dict[str, Any], clan_tag: str) -> bool:
        # CWL win rule: higher stars wins; if tied, higher destruction wins.
        own, opp = self._war_blocks_for_clan(war, clan_tag)
        own_stars = int(own.get("stars") or 0)
        opp_stars = int(opp.get("stars") or 0)
        if own_stars != opp_stars:
            return own_stars > opp_stars
        own_destr = float(own.get("destructionPercentage") or 0.0)
        opp_destr = float(opp.get("destructionPercentage") or 0.0)
        return own_destr > opp_destr


    def _render_cwl_stars_image(
        self,
        clan_name: str,
        clan_tag: str,
        period_label: str,
        rows: List[List[str]],
        bonuses_assignable: int,
    ) -> discord.File:
        # Render a dark stars leaderboard card for the dashboard thread.
        fig_height = max(4.0, 2.0 + 0.30 * max(len(rows), 1))
        fig, ax = plt.subplots(figsize=(11.2, fig_height), dpi=150)
        fig.patch.set_facecolor("#232b38")
        ax.set_facecolor("#232b38")
        ax.axis("off")

        ax.text(
            0.01, 0.975, f"{clan_name} ({clan_tag})",
            transform=ax.transAxes, fontsize=14, fontweight="bold", color="#e6edf3", va="top"
        )
        ax.text(
            0.01, 0.895, f"Clan War League Stars ({period_label})",
            transform=ax.transAxes, fontsize=10.5, color="#8fbcbb", va="top"
        )

        col_labels = ["Rank", "Stars", "Destruction", "Attacks", "TH", "Player"]
        table = ax.table(
            cellText=rows if rows else [["-", "-", "-", "-", "-", "No data"]],
            colLabels=col_labels,
            cellLoc="center",
            colLoc="center",
            colWidths=[0.06, 0.09, 0.12, 0.12, 0.08, 0.45],
            bbox=[0.01, 0.11, 0.98, 0.70],
        )
        table.auto_set_font_size(False)
        table.set_fontsize(9.5)
        table.scale(1, 1.18)

        for (row_idx, col_idx), cell in table.get_celld().items():
            cell.set_edgecolor("#3a455a")
            if row_idx == 0:
                cell.set_facecolor("#334155")
                cell.get_text().set_color("#e6edf3")
                cell.get_text().set_weight("bold")
            else:
                cell.set_facecolor("#2a3445" if row_idx % 2 else "#283142")
                cell.get_text().set_color("#d8dee9")
            if col_idx == 5:
                cell.get_text().set_ha("left")

        bonus_label = "Bonus" if bonuses_assignable == 1 else "Bonuses"
        ax.text(
            0.01, 0.04, f"CWL {period_label} | {bonuses_assignable} {bonus_label} Available",
            transform=ax.transAxes, fontsize=9.5, color="#9aa7b8", va="bottom"
        )

        image = io.BytesIO()
        self._save_matplotlib_image(fig, image)
        plt.close(fig)
        image.seek(0)
        return discord.File(image, filename="cwl_stars_summary.png")


    async def _build_dashboard_stars_payload(
        self,
        clan_code: str,
    ) -> Tuple[discord.Embed, discord.File]:
        # Build stars leaderboard payload for the clan dashboard.
        clan_name = CLAN_NAMES.get(clan_code, clan_code)
        clan_tag = CWL_CLAN_TAGS.get(clan_code, "")
        now = datetime.now(dt_timezone.utc)
        if not self._is_cwl_window():
            image_file = self._render_cwl_stars_image(clan_name, clan_tag or "-", now.strftime("%Y-%m"), [], 0)
            embed = discord.Embed(
                title=f"CWL Stars - {clan_name}",
                description="No CWL season is active right now.",
                color=discord.Color(DEFAULT_EMBED_COLOR_HEX),
                timestamp=now,
            )
            embed.set_thumbnail(url=DEFAULT_THUMBNAIL_URL)
            embed.set_image(url="attachment://cwl_stars_summary.png")
            return embed, image_file

        wars, warnings = await self.bonus_analysis.current_wars(clan_code)

        if not wars or not clan_tag:
            image_file = self._render_cwl_stars_image(clan_name, clan_tag or "-", now.strftime("%Y-%m"), [], 0)
            embed = discord.Embed(
                title=f"CWL Stars - {clan_name}",
                description="No CWL war data is available right now.",
                color=discord.Color(DEFAULT_EMBED_COLOR_HEX),
                timestamp=now,
            )
            if warnings:
                embed.add_field(name="Warnings", value=" | ".join(warnings[:2]), inline=False)
            embed.set_thumbnail(url=DEFAULT_THUMBNAIL_URL)
            embed.set_image(url="attachment://cwl_stars_summary.png")
            return embed, image_file

        ended_wars = [war for war in wars if (war.get("state") or "") == "warEnded"]
        if not ended_wars:
            image_file = self._render_cwl_stars_image(clan_name, clan_tag, now.strftime("%Y-%m"), [], 0)
            embed = discord.Embed(
                title=f"CWL Stars - {clan_name}",
                description="No CWL rounds have finished yet.",
                color=discord.Color.from_str("#6e3c38"),
                timestamp=now,
            )
            if warnings:
                embed.add_field(name="Warnings", value=" | ".join(warnings[:2]), inline=False)
            embed.set_thumbnail(url=DEFAULT_THUMBNAIL_URL)
            embed.set_image(url="attachment://cwl_stars_summary.png")
            return embed, image_file

        player_stats: Dict[str, Dict[str, Any]] = {}
        period_candidates: List[datetime] = []
        league_name = await self._fetch_clan_war_league_name(clan_tag)
        wins_count = 0

        # Use ended rounds only so rankings do not fluctuate with in-progress/prep war payloads.
        for war in ended_wars:
            own_block, _ = self._war_blocks_for_clan(war, clan_tag)
            if not league_name:
                league_name = str((own_block.get("warLeague") or {}).get("name") or "") or None
            start_dt = coc_time_to_dt(war.get("startTime"))
            if start_dt:
                period_candidates.append(start_dt)
            if self._did_clan_win_war(war, clan_tag):
                wins_count += 1

            attacks_per_member = int(war.get("attacksPerMember") or 1)
            for member in own_block.get("members", []) or []:
                player_tag = str(member.get("tag") or "")
                if not player_tag:
                    continue
                stats = player_stats.setdefault(
                    player_tag,
                    {
                        "name": str(member.get("name") or player_tag),
                        "th": int(member.get("townhallLevel") or 0),
                        "stars": 0,
                        "destruction": 0.0,
                        "hits_used": 0,
                        "hits_expected": 0,
                    },
                )
                stats["name"] = str(member.get("name") or stats["name"])
                stats["th"] = max(int(stats.get("th") or 0), int(member.get("townhallLevel") or 0))
                member_attacks = member.get("attacks", []) or []
                stats["hits_used"] += len(member_attacks)
                stats["hits_expected"] += attacks_per_member
                for attack in member_attacks:
                    stats["stars"] += int(attack.get("stars") or 0)
                    stats["destruction"] += float(attack.get("destructionPercentage") or 0.0)

        ordered_rows = sorted(
            player_stats.values(),
            key=lambda row: (
                -int(row["stars"]),
                -float(row["destruction"]),
                -int(row["hits_used"]),
                -int(row["th"]),
                str(row["name"]).lower(),
            ),
        )

        table_rows: List[List[str]] = []
        for idx, row in enumerate(ordered_rows, start=1):
            table_rows.append(
                [
                    str(idx),
                    str(int(row["stars"])),
                    f"{int(round(float(row['destruction'])))}%",
                    f"{int(row['hits_used'])}/{int(row['hits_expected'])}",
                    str(int(row["th"])),
                    str(row["name"])[:26],
                ]
            )

        period_source = min(period_candidates) if period_candidates else now
        period_ym = period_source.strftime("%Y-%m")
        period_text = period_source.strftime("%b %Y")
        guaranteed_slots = self._cwl_guaranteed_bonus_slots(league_name)
        bonuses_assignable = guaranteed_slots + wins_count

        image_file = self._render_cwl_stars_image(
            clan_name,
            clan_tag,
            period_ym,
            table_rows,
            bonuses_assignable,
        )
        embed = discord.Embed(
            title=f"CWL Stars - {clan_name}",
            description=f"Results for {period_text}. **{bonuses_assignable}** bonuses available.",
            color=discord.Color(DEFAULT_EMBED_COLOR_HEX),
            timestamp=now,
        )
        if warnings:
            embed.add_field(name="Warnings", value=" | ".join(warnings[:2]), inline=False)
        embed.set_thumbnail(url=DEFAULT_THUMBNAIL_URL)
        embed.set_image(url="attachment://cwl_stars_summary.png")
        return embed, image_file


    @staticmethod
    def _classify_prep_matchup_by_th(
        own_members: List[Dict[str, Any]],
        opp_members: List[Dict[str, Any]],
    ) -> Tuple[str, float]:
        # Compare matched map positions and classify by average TH edge.
        pair_count = min(len(own_members), len(opp_members))
        if pair_count <= 0:
            return "Even", 0.0

        own_total = 0
        opp_total = 0
        valid_pairs = 0
        for idx in range(pair_count):
            own_th = int(own_members[idx].get("townhallLevel") or 0)
            opp_th = int(opp_members[idx].get("townhallLevel") or 0)
            if own_th <= 0 or opp_th <= 0:
                continue
            own_total += own_th
            opp_total += opp_th
            valid_pairs += 1

        if valid_pairs <= 0:
            return "Even", 0.0

        avg_diff = (own_total - opp_total) / valid_pairs
        if avg_diff >= 0.35:
            return "Easy", avg_diff
        if avg_diff <= -0.35:
            return "Tough", avg_diff
        return "Even", avg_diff


    async def _build_dashboard_prep_embed(
        self,
        clan_code: str,
        wars: List[Dict[str, Any]],
    ) -> Tuple[discord.Embed, discord.File]:
        clan_tag = CWL_CLAN_TAGS.get(clan_code, "")
        prep_wars = [war for war in wars if (war.get("state") or "") == "preparation"]
        now = datetime.now(dt_timezone.utc)
        clan_name = CLAN_NAMES.get(clan_code, clan_code)

        if not prep_wars:
            any_in_war = any((war.get("state") or "") == "inWar" for war in wars)
            any_ended = any((war.get("state") or "") == "warEnded" for war in wars)
            if any_in_war:
                status_text = "No CWL preparation day is active right now. Current round is in Battle Day."
            elif any_ended:
                status_text = "All current CWL rounds appear to be finished."
            else:
                status_text = "No CWL preparation day is active right now."
            image_file = self._render_prep_matchup_image(clan_name, "-", "Preparation Preview", [], status_text)
            embed = discord.Embed(
                title=f"Next Prep Matchup - {clan_name}",
                description=status_text,
                color=discord.Color(DEFAULT_EMBED_COLOR_HEX),
                timestamp=now,
            )
            embed.set_thumbnail(url=DEFAULT_THUMBNAIL_URL)
            embed.set_image(url="attachment://cwl_prep_matchup.png")
            return embed, image_file

        prep_wars.sort(
            key=lambda war: coc_time_to_dt(war.get("startTime")) or (now + timedelta(days=365))
        )
        selected = prep_wars[0]
        own, opp = self._war_blocks_for_clan(selected, clan_tag)
        own_name = own.get("name") or clan_name
        opp_name = opp.get("name") or "Unknown Opponent"
        own_members = sorted((own.get("members", []) or []), key=lambda m: int(m.get("mapPosition") or 999))
        opp_members = sorted((opp.get("members", []) or []), key=lambda m: int(m.get("mapPosition") or 999))
        rows = max(len(own_members), len(opp_members), int(selected.get("teamSize") or 0))
        matchup_label, _ = self._classify_prep_matchup_by_th(own_members, opp_members)

        tags = [m.get("tag") for m in own_members + opp_members if m.get("tag")]
        hero_sums = await self._fetch_hero_sums(tags)
        table_rows: List[List[str]] = []
        for idx in range(rows):
            own_member = own_members[idx] if idx < len(own_members) else {}
            opp_member = opp_members[idx] if idx < len(opp_members) else {}
            own_th = int(own_member.get("townhallLevel") or 0)
            opp_th = int(opp_member.get("townhallLevel") or 0)
            own_hero = hero_sums.get(own_member.get("tag"))
            opp_hero = hero_sums.get(opp_member.get("tag"))
            table_rows.append([
                str(idx + 1),
                str(own_th) if own_th > 0 else "-",
                str(own_hero) if isinstance(own_hero, int) else "--",
                str(opp_th) if opp_th > 0 else "-",
                str(opp_hero) if isinstance(opp_hero, int) else "--",
            ])

        start_dt = coc_time_to_dt(selected.get("startTime"))
        round_no = int(selected.get("_round") or 0)
        round_line = f"Round {round_no}"
        starts_line = f"Starts <t:{int(start_dt.timestamp())}:R>" if start_dt else "Start time unavailable"
        matchup_line = f"Matchup: {matchup_label}"
        status_color = {
            "Easy": "#7bd88f",
            "Tough": "#ff6b6b",
            "Even": "#f0c674",
        }.get(matchup_label, "#f0c674")
        # Image text must be static; Discord timestamp tokens do not render inside PNGs.
        subtitle_image = f"{round_line} | Preparation Matchup"
        image_file = self._render_prep_matchup_image(
            own_name,
            opp_name,
            subtitle_image,
            table_rows,
            status_text=matchup_line,
            status_color=status_color,
        )

        embed = discord.Embed(
            title=f"Next Prep Matchup - {clan_name}",
            description=f"{round_line} • {starts_line}\n{matchup_line}",
            color=discord.Color(DEFAULT_EMBED_COLOR_HEX),
            timestamp=now,
        )
        embed.set_thumbnail(url=DEFAULT_THUMBNAIL_URL)
        embed.set_image(url="attachment://cwl_prep_matchup.png")
        return embed, image_file


    async def _build_prep_preview_payload(
        self,
        clan_code: str,
    ) -> Tuple[discord.Embed, discord.File]:
        now = datetime.now(dt_timezone.utc)
        clan_name = CLAN_NAMES.get(clan_code, clan_code)
        if not self._is_cwl_window():
            status_text = "No CWL matchup to preview right now. This will update when the next CWL starts."
            no_data_embed = discord.Embed(
                title=f"Next Prep Matchup - {clan_name}",
                description=status_text,
                color=discord.Color(DEFAULT_EMBED_COLOR_HEX),
                timestamp=now,
            )
            no_data_embed.set_thumbnail(url=DEFAULT_THUMBNAIL_URL)
            no_data_file = self._render_prep_matchup_image(
                clan_name,
                "-",
                "Preparation Preview",
                [],
                status_text,
            )
            no_data_embed.set_image(url="attachment://cwl_prep_matchup.png")
            return no_data_embed, no_data_file

        wars, warnings = await self.bonus_analysis.current_wars(clan_code)
        if not wars:
            no_data_embed = discord.Embed(
                title=f"Next Prep Matchup - {clan_name}",
                description="No CWL data is available right now.",
                color=discord.Color(DEFAULT_EMBED_COLOR_HEX),
                timestamp=now,
            )
            if warnings:
                no_data_embed.add_field(
                    name="Warnings",
                    value=" | ".join(warnings[:2]),
                    inline=False,
                )
            no_data_embed.set_thumbnail(url=DEFAULT_THUMBNAIL_URL)
            no_data_file = self._render_prep_matchup_image(
                clan_name,
                "-",
                "Preparation Preview",
                [],
                "No CWL data is available right now.",
            )
            no_data_embed.set_image(url="attachment://cwl_prep_matchup.png")
            return no_data_embed, no_data_file
        prep_embed, prep_file = await self._build_dashboard_prep_embed(clan_code, wars)

        if warnings:
            prep_embed.add_field(
                name="Warnings",
                value=" | ".join(warnings[:2]),
                inline=False,
            )
        return prep_embed, prep_file


    async def _resolve_dashboard_thread(self, thread_id: int) -> Optional[discord.Thread]:
        thread = self.bot.get_channel(thread_id)
        if thread is None:
            try:
                thread = await self.bot.fetch_channel(thread_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return None
        if isinstance(thread, discord.Thread):
            return thread
        return None


    async def _upsert_dashboard_for_clan(self, clan_code: str) -> None:
        thread_id = DASHBOARD_THREADS.get(clan_code)
        if not thread_id:
            return
        thread = await self._resolve_dashboard_thread(thread_id)
        if thread is None:
            return

        was_archived = thread.archived
        if was_archived:
            try:
                await thread.edit(archived=False, locked=False)
            except (discord.Forbidden, discord.HTTPException):
                return

        try:
            prep_embed, prep_file = await self._build_prep_preview_payload(clan_code)
            stars_embed, stars_file = await self._build_dashboard_stars_payload(clan_code)

            prep_message_ids = self.dashboard_state.setdefault("prep_message_ids", {})
            stars_message_ids = self.dashboard_state.setdefault("stars_message_ids", {})

            async def upsert_message(
                message_ids: Dict[str, Any],
                embed: discord.Embed,
                image_file: discord.File,
                title_fragment: str,
                view: Optional[discord.ui.View] = None,
            ) -> None:
                message_id = message_ids.get(clan_code)
                target_message: Optional[discord.Message] = None
                state_changed = False
                if isinstance(message_id, int):
                    try:
                        target_message = await thread.fetch_message(message_id)
                    except discord.NotFound:
                        target_message = None
                    except discord.Forbidden:
                        LOGGER.warning(
                            "Dashboard message fetch forbidden for %s (%s) message %s",
                            clan_code,
                            title_fragment,
                            message_id,
                        )
                        raise
                    except discord.HTTPException as e:
                        LOGGER.warning(
                            "Dashboard message fetch failed for %s (%s) message %s: status=%s code=%s",
                            clan_code,
                            title_fragment,
                            message_id,
                            getattr(e, "status", None),
                            getattr(e, "code", None),
                        )
                        raise

                matching_messages = await self._find_dashboard_messages(thread, title_fragment)
                if target_message is None and matching_messages:
                    target_message = matching_messages[0]
                    if message_ids.get(clan_code) != target_message.id:
                        message_ids[clan_code] = target_message.id
                        state_changed = True

                duplicate_messages = [
                    message for message in matching_messages
                    if target_message is not None and message.id != target_message.id
                ]

                if target_message is None:
                    sent = await thread.send(embed=embed, file=image_file, view=view)
                    message_ids[clan_code] = sent.id
                    target_message = sent
                    state_changed = True
                    LOGGER.info(
                        "Created new dashboard message for %s (%s): %s",
                        clan_code,
                        title_fragment,
                        sent.id,
                    )
                else:
                    await target_message.edit(
                        content=None,
                        embed=embed,
                        attachments=[image_file],
                        view=view,
                    )

                for duplicate_message in duplicate_messages:
                    try:
                        await duplicate_message.delete()
                    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                        pass

                if state_changed:
                    self._save_dashboard_state()

            await upsert_message(
                prep_message_ids,
                prep_embed,
                prep_file,
                "Next Prep Matchup",
                view=CwlPrepRefreshView(self, clan_code),
            )
            await upsert_message(
                stars_message_ids,
                stars_embed,
                stars_file,
                "CWL Stars",
                view=CwlPrepRefreshView(self, clan_code),
            )
        finally:
            if was_archived:
                try:
                    await thread.edit(archived=True)
                except (discord.Forbidden, discord.HTTPException):
                    pass


    @tasks.loop(minutes=20)
    async def dashboard_loop(self) -> None:
        # Keep one live prep dashboard message per clan thread.
        if not self.clash_client.configured:
            return
        for clan_code in DASHBOARD_THREADS:
            await self._refresh_dashboard_with_retry(clan_code, context="loop")


    @dashboard_loop.before_loop
    async def before_dashboard_loop(self) -> None:
        await wait_for_boot_complete(self.bot)
        if not self.clash_client.configured:
            return
        for clan_code in DASHBOARD_THREADS:
            await self._refresh_dashboard_with_retry(clan_code, context="startup")
