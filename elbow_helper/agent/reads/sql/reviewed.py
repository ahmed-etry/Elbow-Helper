"""Reviewed notes, closed tables and channel-scoped snapshots that complete the SQL catalogue."""
from contextlib import closing
from dataclasses import replace
from functools import partial
import sqlite3

from elbow_helper.agent.reads.sql.sources import DataSource
from elbow_helper.configuration.channels import OVERSEEING_TERRACE
from elbow_helper.configuration.files import FAMILY_DATABASE

CLOSED = 'feature_access_only'

REVIEWED_NOTES = {
    ('family', 'clan_membership'): 'One observed account-in-clan interval; join account_id=accounts.id and clan_id=clans.id. clan_role is the in-game clan rank, not a Discord role. from_at/to_at/last_observed_at are UTC microseconds. to_at IS NULL selects current membership; from_at<=:at AND (to_at IS NULL OR to_at>:at) selects a dated interval. Gaps are unknown, and first observation is not a verified join date.',
    ('family', 'wars'): "One war shared by both sides; type REG/CWL/FRIENDLY. Filter completed history with state='warEnded' and end_at (UTC microseconds); start_at is battle start. type_basis describes classification certainty. Use participation.account_wars.missed for final missed attacks; completeness flags alone do not prove them.",
    ('family', 'war_members'): 'One account per war side, including opponents; id joins attack member IDs. Filter through wars.end_at (UTC microseconds). townhall is from this war; account_id may be NULL, and player_tag joins accounts.player_tag. For family sides, participation.account_wars returns final missed attacks or NULL with a reason, using complete saved attacks or independent clan totals; an ended war with all attacks used proves zero misses even without complete side evidence.',
}

# Every physical table has a declaration, including tables deliberately closed
# to SQL. Discovery validates this list; it never invents an access grant.
FAMILY_NOTES = {
    'account_aliases': 'Observed account names; account_id joins accounts.id; from_at/to_at bound each observation in UTC microseconds.',
    'account_link_metadata': 'Last observed name, clan and rank for a linked account; account_id joins accounts.id. observed_at is UTC microseconds.',
    'api_field_changes': 'Changed API fields per record_id and observed_at (UTC microseconds); unz(patch_z) reads the JSON patch. Join api_records.id=record_id.',
    'api_records': 'Latest collected API record per source_kind and scope_id; unz(state_z) reads its JSON. Sample times are UTC microseconds.',
    'battle_changes': 'Earlier collected battle state; battle_id joins family_battles.id; unz(prior_state_z) reads its JSON. observed_at is UTC microseconds.',
    'battle_log_coverage': 'Per-player battle polling coverage by origin; failed_spans_json describes gaps. A missing battle is not a verified zero.',
    'battle_strings': 'Shared battle tags, names, modes and army codes; join by id to read value. army(value) decodes an army code.',
    'family_battles': 'One recorded battle per account, battle_at (UTC microseconds), opponent and direction; is_attack=1 selects attacks. Join account_string_id, opponent_string_id, opponent_name_id and battle_type_id separately to battle_strings.id; army_id joins an army code there. unz(resources_z) reads resource JSON.',
    'clan_counters': 'Clan totals as last checked, with which fields were present, as JSON; clan_id joins clans.id; sampled_at is when it was checked, in UTC microseconds.',
    'clan_observations': 'Observed clan metadata JSON; clan_id joins clans.id; observed_at is UTC microseconds.',
    'collection_items': 'One attempted collection item; run_id joins collection_runs.id. Status and completeness describe what was collected; times are UTC microseconds.',
    'collection_runs': 'Collection runs and their attempted, successful, empty and failed item counts; times are UTC microseconds.',
    'current_link_rows': 'Current linked Clash accounts with Discord member ID and last observed name, clan and rank; player_tag joins accounts.player_tag.',
    'cwl_log_summaries': 'One line per clan per CWL week (clan_tag, end_at in UTC microseconds), totals over its rounds, including opponent totals; not individual attack evidence.',
    'cwl_season_sources': 'Clan sources for each collected CWL season; season_id joins cwl_seasons.id; clan_tag joins clans.tag.',
    'donation_snapshots': 'In-game donations and donations received per account_id, as of sampled_at (UTC microseconds); clan_id is the clan the account was in then. season_key may be empty. One row is a running total, not what was donated over a period.',
    'family_api_entities': 'Collected API entity identities and source bounds; source_at/final_at are UTC microseconds. first_run_id joins collection_runs.id.',
    'family_api_state': 'API collection checkpoints, queue state and last outcomes per source_kind and scope_id; times are UTC microseconds.',
    'guild_membership': 'Observed Discord server stays; member_id joins members.id. from_at/to_at and discord_joined_at are UTC microseconds; observation bounds are not always exact join or leave times.',
    'hero_equipment': 'Observed equipment slots per account and hero; account_id joins accounts.id; hero_item_id/equipment_item_id join item_catalog.id; observed_at is UTC microseconds.',
    'item_catalog': 'Player item definitions, names, categories and source identifiers; id joins player_items.item_id and hero_equipment item IDs.',
    'legacy_link_aliases': 'Imported account-name observations; account_id joins accounts.id; observation and import times are UTC microseconds.',
    'legacy_links': 'Linked account tags and Discord member IDs with last observed name, clan and rank; compatibility view of the family record.',
    'member_aliases': 'Observed Discord member names and nicknames; member_id joins members.id; from_at/to_at are UTC microseconds.',
    'old_clan_stays': 'Imported account-in-clan observations; account_id joins accounts.id; clan_tag joins clans.tag. Times are UTC microseconds; gaps and observation bounds do not prove exact membership changes.',
    'old_profile_totals': 'Imported account progression totals; account_id joins accounts.id; observed_at is UTC microseconds; origin identifies the imported source.',
    'old_raid_weekends': 'Imported clan raid-weekend coverage and member counts; clan_tag joins clans.tag; weekend_start/end_at are UTC microseconds.',
    'player_collection_state': 'Player collection checkpoints and last good observations; account_id joins accounts.id; times are UTC microseconds. Queue status is not player activity.',
    'player_counters': 'Account totals as last checked, with which fields were present, as JSON; account_id joins accounts.id; sampled_at is when it was checked, in UTC microseconds. Donation totals can reset.',
    'player_coverage': 'Account observation coverage intervals and completeness JSON; account_id joins accounts.id; times are UTC microseconds.',
    'player_items': 'Observed item levels and presence per account; account_id joins accounts.id, item_id joins item_catalog.id; observed_at is UTC microseconds.',
    'player_observations': 'Observed profile field values as value_json; account_id joins accounts.id; observed_at is UTC microseconds.',
    'player_ranked_references': 'Observed current and previous ranked group/season references per account_id; observed_at is UTC microseconds.',
    'profile_history_coverage': 'Per-account profile-history coverage by origin, with gaps, failures and field coverage; times are UTC microseconds.',
    'raid_member_lines': 'Collected raid participation per clan_tag, weekend_start and player_tag; attack counts and capital loot are observed values. Times are UTC microseconds.',
    'war_collection_state': 'War collection checkpoints and outcomes; war_id joins wars.id; times are UTC microseconds. Collection state is not a war result.',
    'war_legacy_ids': 'Imported war identifiers and capture state; war_id joins wars.id; capture and mapping times are UTC microseconds.',
    'war_source_keys': 'Collected source keys mapped to wars.id through war_id; one war can have several source keys.',
}

EXTRA_TABLES = {
    'family': {name: ('none', note) for name, note in FAMILY_NOTES.items()} | {
        'ignored_tags': ('recruiter_or_core', 'Ignored account tags used by account linking.'),
        'suggestions': ('recruiter_or_core', 'Account-link suggestions with proposed Discord owner and review message IDs.'),
        'member_roles': ('lead', 'Observed role intervals; member_id joins members.id, role_id is a Discord role ID. from_at/to_at are UTC microseconds; these are observation bounds, not guaranteed exact role-change times.'),
        'member_last_messages': (CLOSED, 'Read channel-filtered last-message observations through activity.member_last_messages.'),
        'link_operations': ('recruiter_or_core', 'Account-link changes with requested, before and after JSON, status and UTC-microsecond creation/completion times; ownership history is in account_links.'),
        'imports': (CLOSED, 'Collection import receipts; closed to SQL.'),
        'link_imports': (CLOSED, 'Account-link import receipts; closed to SQL.'),
        'saved_board_evidence': (CLOSED, 'Saved board evidence; use its source-authorized lookup.'),
    },
    'achievements': {'cwl_reward_grants': ('core', 'Recorded CWL coin grants; not proof that a game bonus was awarded.')},
    'rosters': {
        'roster_posts': (CLOSED, 'Roster post destinations; closed to SQL.'),
        'roster_layouts': ('none', 'Saved roster display layouts; not signup or participation records.'),
        'roster_automation_events': ('none', 'Recorded roster schedule actions keyed by roster_id, cycle_key and event_key; claimed_ts is UTC Unix seconds.'),
    },
    'health': {name: (CLOSED, note) for name, note in {
        'workflow_events': 'Read source-filtered events through workflows.<workflow>_events.',
        'workflow_state': 'Combined workflow state; use each workflow\'s source-authorized lookup.',
        'examination_cases': 'Promotion case state; use read_promotion_cases.',
        'feature_documents': 'Feature state documents; only separately declared state datasets are readable.',
        'old_file_copy': 'Settings import receipt; closed to SQL.',
        'storage_move_receipt': 'Storage move receipt; closed to SQL.',
    }.items()},
    'agent': {name: (CLOSED, 'Bot request, conversation or operational records; closed to SQL.') for name in (
        'action_log', 'action_runs', 'action_steps', 'agent_messages', 'conversation_replies',
        'conversations', 'feedback_hint_counter', 'member_timezones', 'meme_usage',
        'operator_debug_records', 'replies', 'reply_reports', 'request_diagnostics', 'requests',
        'research_jobs', 'saved_requests', 'watchers',
    )},
}

# These levels come from workflow_reads.recruitment/promotion. Support,
# lifecycle and transfers authorize by channel, not by a global role group.
WORKFLOW_LEVELS = {'recruitment': 'recruitment', 'examination': 'promotion',
                   'support': 'none', 'lifecycle': 'none', 'transfers': 'none',
                   'hibernation': 'none', 'leadership_news': CLOSED}


def stored_tables(path):
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as connection:
        return {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_schema WHERE type IN ('table','view') AND name NOT GLOB 'sqlite_*'")}


def complete_source(paths, source):
    extras = EXTRA_TABLES.get(source.alias, {})
    known = {name for name, _, _ in source.tables} | extras.keys()
    path = source.resolve(paths)
    if path.is_file():
        unknown = stored_tables(path) - known
        if unknown:
            raise ValueError('Undeclared SQL tables: ' + ', '.join(source.alias + '.' + n for n in sorted(unknown)))
    existing = {name for name, _, _ in source.tables}
    reviewed = tuple((name, level, REVIEWED_NOTES.get((source.alias, name), note))
                     for name, level, note in source.tables)
    return replace(source, tables=reviewed + tuple(
        (name, level, note) for name, (level, note) in extras.items() if name not in existing))


def workflow_snapshot(paths, path, readable, *, channel_ids=frozenset(), read_channels=None):
    if not readable:
        return {}
    tables = {}
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as connection:
        columns = [(row[1], row[2]) for row in connection.execute('PRAGMA table_info(workflow_events)')]
        channel_index = next(i for i, (name, _) in enumerate(columns) if name == 'source_channel_id')
        for name in readable:
            workflow = name.removesuffix('_events')
            rows = []
            for raw in connection.execute('SELECT * FROM workflow_events WHERE workflow=? ORDER BY id', (workflow,)):
                row = list(raw)
                if workflow == 'hibernation':
                    row[channel_index] = OVERSEEING_TERRACE
                channel = row[channel_index]
                if (channel not in channel_ids or
                        (workflow == 'lifecycle' and OVERSEEING_TERRACE not in channel_ids)):
                    continue
                rows.append(tuple(row))
                if read_channels is not None:
                    read_channels.add(channel)
                    if workflow == 'lifecycle':
                        read_channels.add(OVERSEEING_TERRACE)
            tables[name] = (columns, rows)
    return tables


def activity_snapshot(paths, path, readable, *, channel_ids=frozenset(), read_channels=None):
    if 'member_last_messages' not in readable:
        return {}
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as connection:
        columns = [(row[1], row[2]) for row in connection.execute('PRAGMA table_info(member_last_messages)')]
        channel_index = next(i for i, (name, _) in enumerate(columns) if name == 'channel_id')
        rows = [row for row in connection.execute('SELECT * FROM member_last_messages')
                if row[channel_index] in channel_ids]
    if read_channels is not None:
        read_channels.update(row[channel_index] for row in rows)
    return {'member_last_messages': (columns, rows)}


def channel_sources(paths):
    return (
        DataSource('workflows', paths.data_root / 'workflows.sqlite3', tables=tuple(
            (name + '_events', level, 'Recorded ' + name.replace('_', ' ') + ' events; occurred_at/recorded_at are UTC microseconds. Only readable source channels are included; absence is not proof that an event never occurred.'
             if level != CLOSED else 'Leadership news has no SQL read grant; closed to SQL.')
            for name, level in WORKFLOW_LEVELS.items()), snapshot=workflow_snapshot),
        DataSource('activity', FAMILY_DATABASE, tables=(('member_last_messages', 'lead',
            'Latest observed Discord message per member; member_id joins family.members.id; message_at is UTC microseconds. Only channels readable by the asker and this room are included; missing observations do not prove inactivity.'),),
            snapshot=activity_snapshot),
    )


def current_roles_snapshot(paths, path, readable, *, rows=(), command_rows=()):
    tables = {}
    if 'member_roles' in readable:
        tables['member_roles'] = ([('member_id', 'INTEGER'), ('role_id', 'INTEGER'),
                                   ('role_name', 'TEXT')], rows)
    if 'member_commands' in readable:
        tables['member_commands'] = ([('member_id', 'INTEGER'), ('command', 'TEXT'),
                                      ('through_roles', 'TEXT')], command_rows)
    return tables


def current_roles_source():
    return DataSource('discord', FAMILY_DATABASE, tables=(('member_roles', 'none',
        'Current Discord roles, one row per member and role; member_id is a Discord ID and joins '
        'family.members.discord_id or links.links.discord_user_id. role_id and role_name identify '
        'the role. Results with a Discord member ID include role-granted commands visible to the asker. '
        'No history; observed role intervals are in family.member_roles.'),
        ('member_commands', 'none', 'Current role-granted help commands visible to the asker, '
         'one row per member and command. through_roles names the held roles that grant access.')),
        snapshot=current_roles_snapshot)


def scoped_sources(sources, channel_ids, read_channels):
    return tuple(replace(source, snapshot=partial(source.snapshot, channel_ids=channel_ids,
                        read_channels=read_channels))
                 if source.snapshot in (workflow_snapshot, activity_snapshot) else source for source in sources)
