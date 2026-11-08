# Data and migrations

The bot's saved files live under the root `data/` directory. Code for the
family database and workflow history lives in `elbow_helper/data/`.
[Saved data](../reference/storage.md) lists the files and what each holds.

## Family database

`data/store` handles `data/family.sqlite3`. Times are UTC epoch microseconds.
Internal member and account IDs link records together; `members.discord_id`
and `accounts.player_tag` identify them outside the database.

| Tables | Contents |
|---|---|
| `members`, `accounts`, `member_aliases`, `account_aliases` | People, accounts, and their names over time |
| `guild_membership`, `member_roles`, `account_links`, `clan_membership` | Server membership, roles, account ownership, and clan ranks over time; `to_at IS NULL` means it still holds |
| `clan_observations`, `clan_counters`, `donation_snapshots` | Clan details and account counters at the time they were seen |
| `player_observations`, `item_catalog`, `player_items`, `hero_equipment`, `player_counters` | Profile values, named items, equipment, and progress |
| `wars`, `war_sides`, `war_members`, `war_attacks` | War type, both lineups, scores, and individual attacks |
| `cwl_seasons`, `cwl_clans`, `cwl_players`, `cwl_rounds` | League groups, the clans and players in them, and which war belongs to each round |
| `family_api_entities`, `api_records`, `api_field_changes` | Ranked and other Clash API records, with each changed field saved compressed |
| `raid_member_lines`, `member_last_messages` | Raid participation; each Discord member's last message time and channel, without message text |
| `collection_runs`, `collection_items` and the other collection tables | The result of each data check, failures, and when each check is next due |

`FamilyStore.call` runs blocking work one call at a time on a background
thread. `data/reads` goes through it, with a read-only transaction for each
read.

## Workflow history

`data.events.EventsStore` handles `data/workflows.sqlite3`. `workflow_events`
records the workflow, case, kind, time, subject, actor, details, and source
message. A unique event key stops a retry from saving the same event twice.
`workflow_state` holds each workflow's current data; `examination_cases` keeps
one row per examination ticket. A workflow's data and its events save in one
transaction through `EventsStore.call`, on a background thread.

Feature services decide what their data and events mean and check them. Other
code goes through those services or the event readers.
`EventsStore.delete_member` removes a member's workflow history and any open
workflow data that names them; it does not delete their family database
records or Discord transcripts.

Other feature data is kept in `feature_documents` in the same database, through
`infrastructure.persistence.documents`. Health-report overrides and the record
of who changed them are also here. The editable health defaults are
`data/settings/health-defaults.json`.

## Databases and format changes

Achievements and the economy use `data/economy.sqlite3`; rosters use
`data/rosters.sqlite3`. Agent conversations, transcripts, Discord searches,
actions, and schedules share `data/agent.sqlite3`, upgraded by one set of migrations in
`agent/persistence/schema.py`.

The code for each database defines its tables, how rows are read and written,
its version, and the upgrades it supports. Shared SQLite helpers run upgrades
in transactions and refuse a database newer than the code knows. Features that
keep documents define their defaults and checks; a document is replaced in one
SQLite transaction on a background thread.

For a format change, look at the live data and any backup you might restore,
add the smallest upgrade that works, and test an empty database, the current
version, each older version still supported, a failed upgrade, and an unknown
newer version. Keep an upgrade while the live data or a usable backup may
still need it. Update [Saved data](../reference/storage.md) when paths or
contents change.
