"""Formatting and embed helper methods used by war processing."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import discord


def build_war_id(data: Dict[str, Any]) -> str:
    """Return a stable identity for one regular-war instance."""
    end = data.get("endTime") or ""
    prep = data.get("preparationStartTime") or ""
    opponent_tag = (data.get("opponent") or {}).get("tag", "")
    return f"{prep}-{end}-{opponent_tag}"


class HelperMixin:

    def _coctime_to_dt(self, value: Optional[str]) -> Optional[datetime]:
        # Convert CoC time string to aware datetime
        if not value:
            return None
        for fmt in ("%Y%m%dT%H%M%S.%fZ", "%Y%m%dT%H%M%SZ", "%Y%m%dT%H%M%S"):
            try:
                return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
        return None

    def _extra_war_start(self, now: datetime) -> datetime:
        # I Set Extra Wars Start Time To The Last Day Of The Month At 21:00 UTC
        if now.month == 12:
            next_month_first = datetime(year=now.year + 1, month=1, day=1, tzinfo=timezone.utc)
        else:
            next_month_first = datetime(year=now.year, month=now.month + 1, day=1, tzinfo=timezone.utc)
        last_day = next_month_first - timedelta(days=1)
        return last_day.replace(hour=21, minute=0, second=0, microsecond=0, tzinfo=timezone.utc)

    def _cwl_note_for_end(self, ended_at: datetime, *, prep_start: Optional[datetime] = None) -> str:
        anchor_time = ended_at
        if prep_start and (prep_start.year, prep_start.month) != (ended_at.year, ended_at.month):
            anchor_time = prep_start
        extra_start = self._extra_war_start(anchor_time)
        stop_time = extra_start - timedelta(hours=48)
        warn_time = extra_start - timedelta(hours=60)
        if ended_at >= stop_time:
            return "**CWL starting soon; please pause wars.**"
        if ended_at >= warn_time:
            stop_ts = int(stop_time.timestamp())
            return (
                "Start the next war search when you can — "
                f"CWL pause in <t:{stop_ts}:R>."
            )
        return "**Don't forget to start the next war search!**"

    def _build_war_id(self, data: Dict[str, Any]) -> str:
        # Unique identifier per war instance to prevent duplicate summaries
        return build_war_id(data)

    def _compute_missed(self, data: Dict[str, Any]) -> List[str]:
        # Build list of missed attacks from war payload
        members = (data.get("clan") or {}).get("members", []) or []
        per = data.get("attacksPerMember") or 2
        missed = []
        for m in members:
            used = len(m.get("attacks", []) or [])
            remaining = max(0, per - used)
            if remaining:
                name = m.get("name") or "Unknown"
                missed.append(f"{name} ({remaining} missed)")
        return missed

    def _to_superscript(self, value: int) -> str:
        digits = {"0": "⁰", "1": "¹", "2": "²", "3": "³", "4": "⁴", "5": "⁵", "6": "⁶", "7": "⁷", "8": "⁸", "9": "⁹"}
        return "".join(digits.get(ch, ch) for ch in str(max(0, value)))

    def _add_missed_attack_fields(self, embed: discord.Embed, missed: List[str]) -> None:
        if not missed:
            embed.add_field(name="Missed Attacks", value="None recorded", inline=False)
            return

        max_field_value_len = 1024
        max_total_fields = 25
        reserved_fields = 1  # Keep room for the "War Ended" field.
        max_missed_fields = max(1, max_total_fields - len(embed.fields) - reserved_fields)
        field_chunks: List[List[str]] = [[]]
        hidden_count = 0

        for raw_line in missed:
            line = raw_line if len(raw_line) <= max_field_value_len else f"{raw_line[:1021]}..."
            current_chunk = field_chunks[-1]
            candidate_value = "\n".join(current_chunk + [line]) if current_chunk else line
            if len(candidate_value) <= max_field_value_len:
                current_chunk.append(line)
                continue
            if len(field_chunks) < max_missed_fields:
                field_chunks.append([line])
                continue
            hidden_count += 1

        if hidden_count:
            suffix = f"...and {hidden_count} more"
            last_chunk = field_chunks[-1]
            while last_chunk:
                candidate_value = "\n".join(last_chunk + [suffix])
                if len(candidate_value) <= max_field_value_len:
                    break
                last_chunk.pop()
            if last_chunk:
                last_chunk.append(suffix)
            else:
                field_chunks[-1] = [suffix]

        for idx, chunk in enumerate(field_chunks, start=1):
            name = "Missed Attacks" if idx == 1 else f"Missed Attacks{self._to_superscript(idx)}"
            value = "\n".join(chunk) if chunk else "None recorded"
            embed.add_field(name=name, value=value, inline=False)
