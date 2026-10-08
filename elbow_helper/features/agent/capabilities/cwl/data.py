"""Queryable feature state files."""

from pathlib import Path

from elbow_helper.features.cwl.config import (
    ROUTER_STATE_FILE,
    SCHEDULER_STATE_FILE,
    TRANSFER_STATE_FILE,
    DASHBOARD_STATE_FILE,
    BONUS_CONFIG_FILE,
    BONUS_DASHBOARD_STATE_FILE,
)
from elbow_helper.features.cwl.bonus.config import BONUS_CONFIG_AUDIT_FILE

from ...datasets.sources import DataSource


SOURCES = (
    DataSource(
        "state", Path(ROUTER_STATE_FILE),
        state_name="cwl_router", level="lead_plus_or_cwl_helper",
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
        state_name="cwl_scheduler", level="lead_plus_or_cwl_helper",
        note="{sent_keys:[string]}; completed announcement/reminder schedule keys.",
        config_constant="elbow_helper.features.cwl.config.SCHEDULER_STATE_FILE",
    ),
    DataSource(
        "state", Path(TRANSFER_STATE_FILE),
        state_name="cwl_transfer", level="lead_plus_or_cwl_helper",
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
        state_name="cwl_dashboard", level="lead_plus_or_cwl_helper",
        note=(
            "Dashboard posts: {prep_message_ids:{<clan_code>:message_id},"
            "stars_message_ids:{<clan_code>:message_id}}."
        ),
        config_constant="elbow_helper.features.cwl.config.DASHBOARD_STATE_FILE",
    ),
    DataSource(
        "state", Path(BONUS_CONFIG_FILE),
        state_name="cwl_bonus_config", level="lead_plus_or_cwl_helper",
        note=(
            "Scoring rules: {revision:int,clans:{<clan_code>:object},clan_meta:{<clan_code>:{"
            "updated_at_utc:string,updated_by_id:int,updated_by_name:string}}}; "
            "updated_at_utc ISO UTC."
        ),
        config_constant="elbow_helper.features.cwl.config.BONUS_CONFIG_FILE",
    ),
    DataSource(
        "state", Path(BONUS_DASHBOARD_STATE_FILE),
        state_name="cwl_bonus_dashboard", level="lead_plus_or_cwl_helper",
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
        state_name="cwl_bonus_config_audit", level="lead_plus_or_cwl_helper",
        note=(
            "Config history: {entries:[{ts_utc:string,clan_code:string,summary:string,"
            "actor_discord_id:int,actor_display:string,revision:int}]}; ts_utc ISO UTC."
        ),
        config_constant="elbow_helper.features.cwl.bonus.config.BONUS_CONFIG_AUDIT_FILE",
    ),
)
