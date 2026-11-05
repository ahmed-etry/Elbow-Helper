"""Clan stays and raid weekends known only from the old clan health file, beside the record's own."""


def create_old_history_schema(connection):
    # One row per run of hourly captures that saw an account in one family clan. A stay is
    # first to last capture; one the record's own membership takes over ends where that begins.
    connection.execute("""CREATE TABLE old_clan_stays (
        account_id INTEGER NOT NULL REFERENCES accounts(id), clan_tag TEXT NOT NULL,
        from_at INTEGER NOT NULL, to_at INTEGER NOT NULL, last_observed_at INTEGER NOT NULL,
        captures INTEGER NOT NULL CHECK(captures > 0),
        end_basis TEXT NOT NULL CHECK(end_basis IN ('last_capture','record_membership')),
        basis TEXT NOT NULL CHECK(basis='observed_hourly'), origin TEXT NOT NULL CHECK(origin='legacy_health'),
        source_run_id INTEGER NOT NULL REFERENCES collection_runs(id),
        CHECK(from_at <= last_observed_at AND last_observed_at <= to_at), PRIMARY KEY(account_id,from_at))""")
    connection.execute("CREATE INDEX old_clan_stays_clan ON old_clan_stays(clan_tag,from_at,to_at)")
    # Weekends the record never saw: their member lines are in raid_member_lines, the weekend is only here.
    connection.execute("""CREATE TABLE old_raid_weekends (
        clan_tag TEXT NOT NULL, weekend_start INTEGER NOT NULL, end_at INTEGER NOT NULL,
        members INTEGER NOT NULL, origin TEXT NOT NULL CHECK(origin='legacy_health'),
        source_run_id INTEGER NOT NULL REFERENCES collection_runs(id),
        CHECK(end_at > weekend_start), PRIMARY KEY(clan_tag,weekend_start)) WITHOUT ROWID""")
