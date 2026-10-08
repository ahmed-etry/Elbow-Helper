"""Queryable feature state files."""

from pathlib import Path

from elbow_helper.features.event_stats.config import STATE_FILE

from ...datasets.sources import DataSource


SOURCES = (
    DataSource(
        "state",
        Path(STATE_FILE),
        state_name="event_stats",
        level="lead",
        note=(
            "{schema_version:int,events:[{key:string,source:preset|custom,enabled:bool,name:string,"
            "start:string,end:string,timezone:string,grace_period_hours:int,category_id:int,"
            "channel_id:int,position:int}]}; timestamps ISO; schedules may recur."
        ),
        config_constant="elbow_helper.features.event_stats.config.STATE_FILE",
    ),
)
