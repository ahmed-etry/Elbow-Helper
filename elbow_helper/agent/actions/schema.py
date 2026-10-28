"""Initial schema for action and scheduled state."""

import sqlite3


def create_schema(connection: sqlite3.Connection) -> None:
    statements = """
        CREATE TABLE action_runs (
            run_id TEXT PRIMARY KEY,
            guild_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            request_message_id INTEGER NOT NULL,
            requester_id INTEGER NOT NULL,
            confirmer_id INTEGER NOT NULL,
            status TEXT NOT NULL,
            stop_requested INTEGER NOT NULL DEFAULT 0,
            version INTEGER NOT NULL DEFAULT 0,
            lease_owner TEXT,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            reported_at REAL
        );
        CREATE INDEX action_runs_status ON action_runs(status, updated_at);
        CREATE TABLE action_steps (
            run_id TEXT NOT NULL REFERENCES action_runs(run_id),
            step_index INTEGER NOT NULL,
            action_name TEXT NOT NULL,
            action_label TEXT NOT NULL,
            action_class TEXT NOT NULL,
            values_json TEXT NOT NULL,
            preview_json TEXT NOT NULL,
            before_json TEXT,
            after_json TEXT,
            status TEXT NOT NULL,
            outcome_json TEXT,
            started_at REAL,
            finished_at REAL,
            PRIMARY KEY (run_id, step_index)
        );
        CREATE TABLE action_log (
            log_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            step_index INTEGER NOT NULL,
            requester_id INTEGER NOT NULL,
            confirmer_id INTEGER NOT NULL,
            action_name TEXT NOT NULL,
            action_label TEXT NOT NULL,
            action_class TEXT NOT NULL,
            targets_json TEXT NOT NULL,
            before_json TEXT,
            after_json TEXT,
            outcome TEXT NOT NULL,
            executed_at REAL NOT NULL,
            UNIQUE (run_id, step_index)
        );
        CREATE INDEX action_log_requester ON action_log(requester_id, executed_at);
        CREATE INDEX action_log_expiry ON action_log(executed_at);
        CREATE TABLE saved_requests (
            request_id TEXT PRIMARY KEY,
            guild_id INTEGER NOT NULL,
            requester_id INTEGER NOT NULL,
            destination_channel_id INTEGER NOT NULL,
            rule_json TEXT NOT NULL,
            next_run_at REAL,
            status TEXT NOT NULL,
            version INTEGER NOT NULL DEFAULT 0,
            lease_owner TEXT,
            notice_sent INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );
        CREATE TABLE watchers (
            watcher_id TEXT PRIMARY KEY,
            guild_id INTEGER NOT NULL,
            requester_id INTEGER NOT NULL,
            destination_channel_id INTEGER NOT NULL,
            rule_json TEXT NOT NULL,
            next_check_at REAL,
            last_result_json TEXT,
            holding INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL,
            version INTEGER NOT NULL DEFAULT 0,
            lease_owner TEXT,
            notice_sent INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );
        CREATE TABLE member_timezones (
            member_id INTEGER PRIMARY KEY,
            timezone_name TEXT NOT NULL,
            updated_at REAL NOT NULL
        );

        CREATE TABLE agent_messages (
            message_id INTEGER PRIMARY KEY,
            guild_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            requester_id INTEGER NOT NULL,
            created_at REAL NOT NULL,
            deleted_at REAL
        );


        CREATE INDEX agent_messages_channel
        ON agent_messages(guild_id, channel_id, created_at);
    """
    for statement in statements.split(";"):
        if statement.strip():
            connection.execute(statement)


