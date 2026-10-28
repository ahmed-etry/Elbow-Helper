"""Queryable feature state files."""

from pathlib import Path
import re

from elbow_helper.features.cwl.config import (
    ROUTER_STATE_FILE,
    SCHEDULER_STATE_FILE,
    TRANSFER_STATE_FILE,
    DASHBOARD_STATE_FILE,
    BONUS_CONFIG_FILE,
    BONUS_DASHBOARD_STATE_FILE,
    CWL_HQ_CHANNEL_ID,
)
from elbow_helper.features.cwl.bonus.config import BONUS_CONFIG_AUDIT_FILE

from ...datasets.sources import DataSource


def _bonus_evidence_channels(data):
    """A raw board document carries every board and original evidence source."""
    if not isinstance(data, dict) or not isinstance(data.get("boards", {}), dict):
        raise ValueError("Invalid bonus board state")
    identifiers = {CWL_HQ_CHANNEL_ID}
    for board in data.get("boards", {}).values():
        if not isinstance(board, dict) or not isinstance(board.get("clans", {}), dict):
            raise ValueError("Invalid bonus board")
        sources = [board.get("channel_id")]
        for clan in board.get("clans", {}).values():
            if not isinstance(clan, dict):
                raise ValueError("Invalid bonus review")
            sources.append(clan.get("source_channel_id"))
            source_url = clan.get("source_url") or ""
            if not isinstance(source_url, str):
                raise ValueError("Invalid bonus source URL")
            sources.extend(int(channel) for channel in re.findall(
                r"https?://(?:(?:canary|ptb)\.)?discord(?:app)?\.com/channels/\d+/(\d+)",
                source_url,
            ))
        for identifier in sources:
            if identifier is None:
                continue
            if type(identifier) is not int or identifier <= 0:
                raise ValueError("Invalid bonus evidence channel")
            identifiers.add(identifier)
    return identifiers


SOURCES = (
    DataSource(
        "state", Path(ROUTER_STATE_FILE),
        state_name="cwl_router", level="none",
        note=(
            "CWL roster/rotation cache: {rosters:{<clan_code>:{<war_tag>:[player_tag]}},"
            "roster_names:{<clan_code>:{<war_tag>:{<player_tag>:name}}},"
            "last_war_tag:{<clan_code>:tag},"
            "missed_posted:{<clan_code:war_tag>:bool},missed_pending:{<clan_code:war_tag>:object},"
            "last_poll_ts:int,name_cache:{<clan_code>:{<player_tag>:name}},"
            "cwl_board_messages:{<clan_code>:{channel:int,message:int}}}; "
            "last_poll_ts Unix seconds UTC."
        ),
        config_constant="elbow_helper.features.cwl.config.ROUTER_STATE_FILE",
    ),
    DataSource(
        "state", Path(SCHEDULER_STATE_FILE),
        state_name="cwl_scheduler", level="none",
        note="{sent_keys:[string]}; completed announcement/reminder schedule keys.",
        config_constant="elbow_helper.features.cwl.config.SCHEDULER_STATE_FILE",
    ),
    DataSource(
        "state", Path(TRANSFER_STATE_FILE),
        state_name="cwl_transfer", level="none",
        note=(
            "Transfer checkpoints: {hub_message_id:int|null,"
            "released_roster_cycles:{<roster_id>:cycle_id},"
            "reminder_messages:[{channel_id:int,message_id:int,delete_at:string}]}; "
            "delete_at ISO UTC."
        ),
        config_constant="elbow_helper.features.cwl.config.TRANSFER_STATE_FILE",
    ),
    DataSource(
        "state", Path(DASHBOARD_STATE_FILE),
        state_name="cwl_dashboard", level="none",
        note=(
            "Dashboard posts: {prep_message_ids:{<clan_code>:message_id},"
            "stars_message_ids:{<clan_code>:message_id}}."
        ),
        config_constant="elbow_helper.features.cwl.config.DASHBOARD_STATE_FILE",
    ),
    DataSource(
        "state", Path(BONUS_CONFIG_FILE),
        state_name="cwl_bonus_config", level="none",
        note=(
            "Scoring rules: {revision:int,clans:{<clan_code>:object},clan_meta:{<clan_code>:{"
            "updated_at_utc:string,updated_by_id:int,updated_by_name:string}}}; "
            "updated_at_utc ISO UTC."
        ),
        config_constant="elbow_helper.features.cwl.config.BONUS_CONFIG_FILE",
    ),
    DataSource(
        "state", Path(BONUS_DASHBOARD_STATE_FILE),
        state_name="cwl_bonus_dashboard", level="cwl",
        evidence_channels=_bonus_evidence_channels,
        note=(
            "Bonus boards: {boards:{<mode:month_key>:{mode:string,month_key:int,channel_id:int,"
            "message_id:int|null,closed:bool,created_at:int,updated_at:int,clans:{<clan_code>:{"
            "status:string,recipient_ids:[member_id],skip_report:[object],completed_at:int|null,"
            "source_type:string|null,source_message_id:int|null,source_channel_id:int|null,"
            "source_url:string|null,source_text:string|null}}}}}; "
            "created_at,updated_at,completed_at Unix seconds UTC; month_key=year*12+month."
        ),
        config_constant="elbow_helper.features.cwl.config.BONUS_DASHBOARD_STATE_FILE",
    ),
    DataSource(
        "state", BONUS_CONFIG_AUDIT_FILE,
        state_name="cwl_bonus_config_audit", level="none",
        note=(
            "Config history: {entries:[{ts_utc:string,clan_code:string,summary:string,"
            "actor_discord_id:int,actor_display:string,revision:int}]}; ts_utc ISO UTC."
        ),
        config_constant="elbow_helper.features.cwl.bonus.config.BONUS_CONFIG_AUDIT_FILE",
    ),
)
