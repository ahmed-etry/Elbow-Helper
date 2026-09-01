"""CWL rotation and missed-attack routing."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime
from datetime import timezone as dt_timezone
from typing import Any
from typing import Dict
from typing import List
from typing import Optional
from typing import Set

import discord
from discord.ext import commands

from elbow_helper.domain.player_tags import encode_clash_tag
from elbow_helper.configuration.clans import CLAN_CWL_HELPER_ROLE_IDS
from elbow_helper.configuration.clans import CLAN_NAMES
from elbow_helper.configuration.style import DEFAULT_EMBED_COLOR_HEX
from elbow_helper.configuration.style import DEFAULT_THUMBNAIL_URL
from .helpers import coc_time_to_dt
from .helpers import wait_for_boot_complete
from elbow_helper.infrastructure.persistence import read_json
from elbow_helper.infrastructure.persistence import write_json_atomic_async
from .config import CWL_CLAN_TAGS
from .config import CWL_THREADS
from .config import ROUTER_STATE_FILE


LOGGER = logging.getLogger(__name__)
timezone = dt_timezone


class CwlRouterMixin:
    def _load_state(self) -> Dict:
        try:
            if ROUTER_STATE_FILE.exists():
                data = read_json(ROUTER_STATE_FILE)
                if not isinstance(data, dict):
                    raise TypeError("router state root must be an object")
                for key in ("rosters", "roster_names", "last_war_tag", "missed_posted", "name_cache"):
                    if not isinstance(data.get(key), dict):
                        data[key] = {}
                try:
                    data["last_poll_ts"] = int(data.get("last_poll_ts", int(time.time())))
                except (TypeError, ValueError):
                    data["last_poll_ts"] = int(time.time())
                raw_board_messages = data.get("cwl_board_messages")
                board_messages: Dict[str, Dict[str, int]] = {}
                if isinstance(raw_board_messages, dict):
                    for clan_code, entry in raw_board_messages.items():
                        if not isinstance(clan_code, str) or not isinstance(entry, dict):
                            continue
                        channel_id = entry.get("channel")
                        message_id = entry.get("message")
                        if isinstance(channel_id, int) and isinstance(message_id, int):
                            board_messages[clan_code] = {
                                "channel": channel_id,
                                "message": message_id,
                            }
                data["cwl_board_messages"] = board_messages
                return data
        except (OSError, json.JSONDecodeError, TypeError) as e:
            LOGGER.warning("Failed loading router state: %s", e)
        # rosters/last_war_tag: cache of current CWL war rosters per clan to diff changes
        # missed_posted: tracks which war tags already logged missed attacks via API
        return {
            "rosters": {},
            "roster_names": {},
            "last_war_tag": {},
            "missed_posted": {},
            "last_poll_ts": int(time.time()),
            "name_cache": {},
            "cwl_board_messages": {},
        }


    async def _save_state(self):
        # Persist state to disk; silent on failure to avoid crashing the loop
        try:
            await write_json_atomic_async(ROUTER_STATE_FILE, self.state, indent=2)
        except (OSError, TypeError) as e:
            LOGGER.warning("Failed saving router state: %s", e)


    async def _fetch_json(self, path: str) -> Optional[Dict]:
        if not self.clash_client.configured:
            return None
        response = await self.clash_client.get(
            path,
            attempts=1,
            timeout_seconds=15,
        )
        return response.payload_object if response.status == 200 else None


    async def _get_league_wars(self, clan_key: str) -> List[Dict]:
        """Return all CWL wars for this clan (preparation/inWar/warEnded)."""
        tag = CWL_CLAN_TAGS.get(clan_key)
        if not tag:
            return []
        now_ts = time.time()
        cached_group = self._leaguegroup_cache.get(clan_key)
        war_tags: List[tuple[int, str]] = []
        if cached_group and cached_group.get("war_tags") and now_ts - cached_group["fetched_at"] < 300:
            war_tags = cached_group["war_tags"]
        else:
            group = await self._fetch_json(
                f"/clans/{encode_clash_tag(tag)}/currentwar/leaguegroup",
            )
            if group and "rounds" in group:
                round_index = 1
                for round_data in group.get("rounds", []) or []:
                    for war_tag in round_data.get("warTags", []) or []:
                        if war_tag and war_tag != "#0":
                            war_tags.append((round_index, war_tag))
                    round_index += 1
                if war_tags:
                    self._leaguegroup_cache[clan_key] = {
                        "war_tags": war_tags,
                        "fetched_at": now_ts,
                    }
            elif cached_group and cached_group.get("war_tags"):
                war_tags = cached_group["war_tags"]
            else:
                return []
        # Walk rounds in order so we can tag the round number on matching wars.
        candidates: List[Dict] = []
        total_rounds = max((round_index for round_index, _ in war_tags), default=0)
        for round_index, war_tag in war_tags:
            cached_war = self._war_cache.get(war_tag)
            if cached_war and cached_war.get("_state") == "warEnded":
                cached_war["_total_rounds"] = total_rounds
                candidates.append(cached_war)
                continue
            war = await self._fetch_json(
                f"/clanwarleagues/wars/{encode_clash_tag(war_tag)}",
            )
            if not war:
                if cached_war:
                    cached_war["_total_rounds"] = total_rounds
                    candidates.append(cached_war)
                continue
            state = war.get("state")
            if state not in {"preparation", "inWar", "warEnded"}:
                continue
            # Ensure the target clan participates in this war payload.
            clan_data = war.get("clan", {})
            opp_data = war.get("opponent", {})
            if clan_data.get("tag") != tag and opp_data.get("tag") != tag:
                continue
            war["_warTag"] = war_tag
            war["_round"] = round_index
            war["_state"] = state
            war["_start_dt"] = coc_time_to_dt(war.get("startTime"))
            war["_total_rounds"] = total_rounds
            self._war_cache[war_tag] = war
            candidates.append(war)

        return candidates


    def _extract_complete_roster(
        self,
        war: Dict,
        clan_tag: str,
    ) -> Optional[Tuple[Set[str], Dict[str, str]]]:
        """Return one complete CWL roster snapshot or reject the payload."""
        clan_block = war.get("clan", {})
        if clan_block.get("tag") != clan_tag:
            clan_block = war.get("opponent", {})
        raw_members = clan_block.get("members")
        team_size = war.get("teamSize")
        returned_count = len(raw_members) if isinstance(raw_members, list) else 0
        valid_members = (
            [
                member
                for member in raw_members
                if isinstance(member, dict) and str(member.get("tag") or "").strip()
            ]
            if isinstance(raw_members, list)
            else []
        )
        roster = {
            str(member.get("tag") or "").strip().upper()
            for member in valid_members
        }
        complete = (
            clan_block.get("tag") == clan_tag
            and not isinstance(team_size, bool)
            and isinstance(team_size, int)
            and team_size > 0
            and returned_count == team_size
            and len(valid_members) == team_size
            and len(roster) == team_size
        )
        if not complete:
            LOGGER.warning(
                "CWL rotation roster incomplete clan_tag=%s war=%s returned=%s team_size=%s",
                clan_tag,
                war.get("_warTag") or "unknown",
                returned_count,
                team_size,
            )
            return None

        names = {
            str(member.get("tag") or "").strip().upper(): str(
                member.get("name") or member.get("tag") or "Unknown"
            )
            for member in valid_members
        }
        return roster, names


    def _compute_missed_from_war(self, war: Dict, clan_tag: str) -> List[str]:
        # Build missed-attack strings from the war payload
        clan_block = war.get("clan", {})
        if clan_block.get("tag") != clan_tag:
            clan_block = war.get("opponent", {})
        members = clan_block.get("members", []) or []
        per = war.get("attacksPerMember") or 1
        missed: List[str] = []
        for m in members:
            used = len(m.get("attacks", []) or [])
            remaining = max(0, per - used)
            if remaining:
                name = m.get("name") or m.get("tag") or "Unknown"
                missed.append(f"{name} ({remaining} missed)")
        return missed


    async def _poll_rosters(self):
        # Background loop: poll CoC API and act on roster/missed data.
        await wait_for_boot_complete(self.bot)
        if not self.clash_client.configured:
            LOGGER.warning("Clash API access is not configured; rotation polling disabled.")
        while True:
            try:
                if self.clash_client.configured:
                    await self._poll_once()
            except (asyncio.TimeoutError, discord.HTTPException, RuntimeError) as e:
                LOGGER.exception("roster poll error: %s", e)
            except Exception as e:
                LOGGER.exception("unexpected roster poll error: %s", e)
            await asyncio.sleep(60)


    async def _poll_once(self):
        # One pass over all clans to log rotations and missed attacks.
        # Only poll during CWL window (1st-11th UTC)
        today = datetime.now(timezone.utc).day
        if today > 11 or today < 1:
            return
        last_poll_ts = self.state.get("last_poll_ts", 0)
        poll_ts = int(datetime.now(timezone.utc).timestamp())
        state_dirty = False
        # Track roster changes and missed attacks per clan and war tag
        rosters = self.state.setdefault("rosters", {})
        roster_names = self.state.setdefault("roster_names", {})
        name_cache = self.state.setdefault("name_cache", {})
        last_tags = self.state.setdefault("last_war_tag", {})
        missed_posted = self.state.setdefault("missed_posted", {})
        for clan_key, tag in CWL_CLAN_TAGS.items():
            wars = await self._get_league_wars(clan_key)
            if not wars:
                continue
            await self._sync_cwl_channel(clan_key, wars)
            now = datetime.now(timezone.utc)
            in_war = next((w for w in wars if w.get("_state") == "inWar"), None)
            # Rotation logging needs prep rosters: prefer nearest future prep war,
            # otherwise fall back to the latest prep snapshot available.
            prep_wars = [w for w in wars if w.get("_state") == "preparation"]
            prep_war = None
            if prep_wars:
                future = [w for w in prep_wars if w.get("_start_dt") and w["_start_dt"] > now]
                if future:
                    prep_war = min(future, key=lambda w: w["_start_dt"])
                else:
                    min_dt = datetime.min.replace(tzinfo=timezone.utc)
                    prep_war = max(prep_wars, key=lambda w: w.get("_start_dt") or min_dt)
            active_war = in_war or prep_war or wars[0]
            state = active_war.get("_state") or active_war.get("state")
            war_tag = active_war.get("_warTag")
            prev_tag = last_tags.get(clan_key)

            if war_tag and war_tag != prev_tag:
                last_tags[clan_key] = war_tag
                state_dirty = True

            # Process any ended wars for missed attacks.
            ended_wars = [w for w in wars if w.get("_state") == "warEnded" and w.get("_warTag")]
            ended_candidates: List[tuple[int, Dict[str, Any]]] = []
            for ended in ended_wars:
                end_dt = coc_time_to_dt(ended.get("endTime"))
                if not end_dt:
                    continue
                end_ts = int(end_dt.timestamp())
                if end_ts <= last_poll_ts:
                    continue
                ended_candidates.append((end_ts, ended))
            if ended_candidates:
                for end_ts, ended in sorted(ended_candidates, key=lambda item: item[0]):
                    ended_tag = ended.get("_warTag")
                    if not ended_tag:
                        continue
                    posted_key = f"{clan_key}:{ended_tag}"
                    if posted_key in missed_posted:
                        continue
                    missed = self._compute_missed_from_war(ended, tag)
                    if missed:
                        await self._log_missed_api(
                            clan_key,
                            ended_tag,
                            missed,
                            round_number=ended.get("_round"),
                            end_ts=end_ts,
                        )
                    missed_posted[posted_key] = True
                    state_dirty = True

            rotation_war = prep_war if prep_war is not None else (active_war if state == "preparation" else None)
            rotation_tag = rotation_war.get("_warTag") if rotation_war else None
            rotation_round = rotation_war.get("_round") if rotation_war else None
            if not rotation_war or not rotation_tag:
                continue

            # Diff roster vs prior snapshot and log changes
            roster_snapshot = self._extract_complete_roster(rotation_war, tag)
            if roster_snapshot is None:
                continue
            current, name_map = roster_snapshot
            clan_name_cache = name_cache.setdefault(clan_key, {})
            if name_map:
                clan_name_cache.update(name_map)
            clan_rosters = rosters.setdefault(clan_key, {})
            clan_roster_names = roster_names.setdefault(clan_key, {})
            prev = set(clan_rosters.get(rotation_tag, []))
            if not prev:
                clan_rosters[rotation_tag] = list(current)
                clan_roster_names[rotation_tag] = name_map
                state_dirty = True
                continue
            added = sorted(current - prev)
            removed = sorted(prev - current)
            if added or removed:
                prev_name_map = clan_roster_names.get(rotation_tag, {})
                added_names = [
                    name_map.get(member, prev_name_map.get(member, clan_name_cache.get(member, member)))
                    for member in added
                ]
                removed_names = [
                    prev_name_map.get(member, name_map.get(member, clan_name_cache.get(member, member)))
                    for member in removed
                ]
                await self._log_rotation_api(
                    clan_key,
                    added_names,
                    removed_names,
                    round_number=rotation_round,
                )
                clan_rosters[rotation_tag] = list(current)
                clan_roster_names[rotation_tag] = name_map
                state_dirty = True
        self.state["last_poll_ts"] = poll_ts
        state_dirty = True
        if state_dirty:
            await self._save_state()


    async def _log_rotation_api(
        self,
        clan: str,
        added_names: List[str],
        removed_names: List[str],
        *,
        round_number: Optional[int] = None,
    ):
        # Send roster delta snapshot to the clan thread
        thread_id = CWL_THREADS.get(clan)
        if not thread_id:
            return
        thread = self.bot.get_channel(thread_id)
        if thread is None:
            try:
                thread = await self.bot.fetch_channel(thread_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return
        if not isinstance(thread, (discord.TextChannel, discord.Thread)):
            return
        clan_name = CLAN_NAMES.get(clan, clan)
        title = f"CWL Rotations — {clan_name}"
        if round_number:
            round_line = f"CWL Round {round_number}"
        else:
            round_line = "CWL Round"
        embed = discord.Embed(
            title=title,
            description=round_line,
            color=discord.Color(DEFAULT_EMBED_COLOR_HEX),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_thumbnail(url=DEFAULT_THUMBNAIL_URL)
        embed.add_field(
            name="Members Added",
            value="\n".join(added_names) if added_names else "None",
            inline=False,
        )
        embed.add_field(
            name="Members Removed",
            value="\n".join(removed_names) if removed_names else "None",
            inline=False,
        )
        try:
            await thread.send(embed=embed)
        except (discord.Forbidden, discord.HTTPException) as e:
            LOGGER.warning("Failed to post CWL rotation embed for %s: %s", clan, e)


    async def _log_missed_api(
        self,
        clan: str,
        war_tag: Optional[str],
        users: List[str],
        *,
        round_number: Optional[int] = None,
        end_ts: Optional[int] = None,
    ):
        # Send missed-attack summary with helper ping
        thread_id = CWL_THREADS.get(clan)
        if not thread_id:
            return
        thread = self.bot.get_channel(thread_id)
        if thread is None:
            try:
                thread = await self.bot.fetch_channel(thread_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return
        if not isinstance(thread, (discord.TextChannel, discord.Thread)):
            return
        header = f"CWL Round {round_number}" if round_number else "CWL Round"
        if end_ts:
            header = f"{header} - Ended <t:{end_ts}:f>"
        embed = discord.Embed(
            title=f"Missed Attacks - {header}",
            description="\n".join(users) if users else "None",
            color=discord.Color(DEFAULT_EMBED_COLOR_HEX),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_thumbnail(url=DEFAULT_THUMBNAIL_URL)
        helper_role_id = CLAN_CWL_HELPER_ROLE_IDS.get(clan)
        content = f"<@&{helper_role_id}>" if helper_role_id else None
        try:
            await thread.send(content=content, embed=embed)
        except (discord.Forbidden, discord.HTTPException) as e:
            LOGGER.warning(
                "Failed to post CWL missed-attacks embed for %s (%s): %s",
                clan,
                war_tag or "unknown-war",
                e,
            )


    @commands.Cog.listener()
    async def on_ready(self):
        # Start polling once the bot is ready
        if self._poll_task is None or self._poll_task.done():
            self._poll_task = asyncio.create_task(self._poll_rosters())
