"""Queryable feature state files."""

from pathlib import Path

from elbow_helper.features.role_connections.config import STATE_FILE

from ...datasets.sources import DataSource


SOURCES = (
    DataSource(
        "state",
        Path(STATE_FILE),
        state_name="role_connections",
        level="lead",
        note=(
            "{connections:[{id:string,target_role_id:int,all:[{has:role_id}|{not:role_id}],"
            "any:[{has:role_id}|{not:role_id}]}]}; role connection rules."
        ),
        config_constant="elbow_helper.features.role_connections.config.STATE_FILE",
    ),
)
