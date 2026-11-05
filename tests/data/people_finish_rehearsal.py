"""Offline current-link parity on the supplied record and synthetic old links.

Run from the worktree: python -m tests.data.people_finish_rehearsal.
No model, bot connection, Clash client or external service is started.
"""

import asyncio
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from elbow_helper.app import create_bot
from elbow_helper.core.paths import ApplicationPaths
from elbow_helper.configuration.clans import CLANS
from elbow_helper.configuration.files import ACCOUNT_LINKS_DATABASE, FAMILY_DATABASE
from elbow_helper.configuration.roles import ELDER_ROLE_ID
from elbow_helper.data.reads.profiles import leaf_state, roster_fields
from elbow_helper.data.store import now_us
from elbow_helper.data.store.clans import collect_clans
from elbow_helper.data.store.legacy_links import FIELDS, legacy_shape
from elbow_helper.features.account_links.cog import AccountLinks
from elbow_helper.features.rosters.services.accounts import RosterAccountDirectory
from elbow_helper.agent.capabilities.account_links.data import SOURCES
from elbow_helper.agent.capabilities.account_links.reads import read_linked_accounts
from elbow_helper.agent.datasets.query import run_query
from elbow_helper.agent.families import account_names, identity_reads
from elbow_helper.infrastructure.clash import ClashResponse

from .link_fixtures import FrozenLegacyLinks


def prepare(connection, legacy, at):
    # This supplied record already passed an earlier people rehearsal. Reset
    # only its local gate so production startup exercises the synthetic source;
    # existing ownership intervals remain and are closed by the real importer.
    with connection:
        connection.execute("DELETE FROM imports WHERE topic='people_membership'")
    rosters = {}
    for clan in CLANS.values():
        fields = leaf_state(connection, 'clan_profile', clan.tag, at)
        members = list(roster_fields(connection, clan.tag, at).values())
        rosters[clan.tag] = {'tag': clan.tag, 'name': fields.get('/name') or clan.name,
                             'members': len(members), 'memberList': members,
                             'badgeUrls': {size: fields.get('/badgeUrls/' + size) for size in ('small', 'medium', 'large')}}
    tags = [row['tag'] for roster in rosters.values() for row in roster['memberList']]
    if len(tags) < 3:
        raise AssertionError('The supplied record needs three stored family accounts')
    selected = [tags[1], tags[0], tags[2], '#V0', '#V2', '#V8']
    rows = []
    for index, tag in enumerate(selected):
        raw = tag.lower().lstrip('#') if index == 2 else tag
        rows.append(dict(zip(FIELDS, (raw, 100 + index // 2, ['é', None, '', 'a\0suffix', 'A', 'Z'][index],
                                      None if index == 1 else '', '', 'member'))))
    legacy.parent.mkdir(parents=True, exist_ok=True)
    if not legacy.resolve().is_relative_to(Path.cwd().resolve() / '_data'):
        raise AssertionError('Synthetic links must stay inside the worktree data copy')
    legacy.unlink(missing_ok=True)
    with closing(sqlite3.connect(legacy)) as old, old:
        legacy_shape(old)
        old.execute('PRAGMA user_version=1')
        old.executemany('INSERT INTO links VALUES (?,?,?,?,?,?)', [tuple(row.values()) for row in rows])
        old.execute("INSERT INTO suggestions VALUES ('#V9','Synthetic','BEH','#PP',100,'Synthetic',1,2)")
        old.execute("INSERT INTO ignored_tags VALUES ('#VY')")
    return rosters, rows


async def rehearse():
    root = Path.cwd()
    paths = ApplicationPaths.from_project_root(root)
    paths = replace(paths, data_root=root / '_data')
    if paths.data_root / FAMILY_DATABASE != root / '_data/family/family.sqlite3':
        raise AssertionError('Rehearsal path must stay in this worktree')
    # Use real application assembly and its startup callback, without connecting.
    client = SimpleNamespace(get=AsyncMock(side_effect=AssertionError('No live Clash request')), configured=False)
    bot = create_bot(paths, client, SimpleNamespace(), SimpleNamespace(), SimpleNamespace())
    store, service = bot.link_service.store, bot.link_service
    await asyncio.to_thread(shutil.copy2, Path('D:/Elbow-Helper-wt/warsimport/_data/family/family.sqlite3'), store.path)
    await store.initialize()
    at = now_us()
    legacy = paths.data_root / ACCOUNT_LINKS_DATABASE
    rosters, rows = await store.call(prepare, legacy, at)
    clans = tuple(CLANS.values())
    specs = [(kind, clan.tag) for clan in clans for kind in ('clan_profile', 'clan_roster')]
    run = await store.begin_run('clans', 'offline_frozen_roster_replay', specs, at)
    responses = [ClashResponse(200, rosters[clan.tag], {}, 1, 0) for clan in clans]
    await store.call(collect_clans, run, clans, responses, at)
    await store.finish(run)
    old_bytes = await asyncio.to_thread(legacy.read_bytes)
    await bot.start_links_topic()
    if not service._started:
        raise AssertionError('The production startup did not import people')
    old = FrozenLegacyLinks(legacy)
    new = AccountLinks(bot, client)
    await new._rebuild_snapshots()
    ids = [100, 101, 102, 999]
    members = [SimpleNamespace(id=value, name=f'Synthetic {value}', display_name=f'Synthetic {value}',
                              nick=None, bot=False, roles=[SimpleNamespace(id=ELDER_ROLE_ID, name='Elder')]) for value in ids]
    by_id = {member.id: member for member in members}
    guild = SimpleNamespace(members=members, chunked=True, get_member=by_id.get)
    bot.get_guild = lambda value: guild
    baseline = AccountLinks(bot, client)
    baseline.get_all_links = old.get_all_links
    baseline._clan_members = {clan.code: {m['tag']: {
        'player_tag': m['tag'], 'player_name': m['name'], 'clan_code': clan.code, 'clan_tag': clan.tag,
        'townhall': int(m.get('townHallLevel') or 0), 'role': m['role']}
        for m in rosters[clan.tag]['memberList']} for clan in clans}
    baseline._player_locations = {tag: row for roster in baseline._clan_members.values() for tag, row in roster.items()}
    old.get_player_location = baseline.get_player_location
    comparisons, differences = [], []

    def compare(reader, before, after):
        comparisons.append(reader)
        if before != after:
            differences.append({'reader': reader, 'old': before, 'new': after,
                                'cause': 'unexpected projection difference'})

    selectors = {
        'get_all_links': (), 'get_link_by_tag': (rows[0]['player_tag'],),
        'get_links_by_tags': ([*(row['player_tag'] for row in rows), '#MISSING'],),
        'get_links_for_user': (100,), 'get_links_for_members': (ids,)}
    # Each caller in the before-inventory is exercised through its unchanged
    # source contract, including commands, reviews, recruitment and all agents.
    inventory = (root / '_peoplefinish/readers-before.txt').read_text(encoding='utf-8').splitlines()
    for entry in inventory:
        for method, args in selectors.items():
            if method in entry and 'def ' not in entry:
                before = await asyncio.to_thread(getattr(old, method), *args)
                after = await asyncio.to_thread(getattr(new, method), *args)
                compare(entry, before, after)
                if isinstance(before, dict):
                    compare(entry + '/order', list(before), list(after))
    for member in ids:
        compare(f'rosters/member/{member}', await asyncio.to_thread(RosterAccountDirectory(old).for_member, member),
                await asyncio.to_thread(RosterAccountDirectory(new).for_member, member))
    for clan in clans:
        compare('missing_elder/' + clan.code, await asyncio.to_thread(baseline.get_missing_elder_rows, clan.code),
                await asyncio.to_thread(new.get_missing_elder_rows, clan.code))
        compare('roster/' + clan.code, baseline._clan_members[clan.code], new._clan_members[clan.code])
    def context(source):
        return SimpleNamespace(account_links=source, guild=guild,
            cwl_queries=SimpleNamespace(account_names=lambda: []),
            state=SimpleNamespace(resolved_members={}, candidate_member_ids=set()))
    async def clash(_context, arguments):
        return {'items': [{'tag': tag, 'status': 'ok', 'data': rosters.get(tag, {'tag': tag, 'name': tag}),
                           'observed_at': at} for tag in arguments['tags']]}
    with (patch('elbow_helper.agent.capabilities.account_links.reads.require_evidence_access', AsyncMock()),
          patch.object(identity_reads, 'require_evidence_access', AsyncMock()),
          patch.object(account_names, 'require_evidence_access', AsyncMock()),
          patch.object(identity_reads, 'read_clash', clash), patch.object(account_names, 'read_clash', clash)):
        for name, operation, args in (
                ('agent/linked_accounts', read_linked_accounts, {'member_ids': ids}),
                ('agent/member_accounts', identity_reads.member_accounts, {'member_ids': ids, 'include_roles': True}),
                ('agent/account_names', account_names.resolve_account_names, ['A', 'Z', 'é'])):
            compare(name, await operation(context(old), args), await operation(context(new), args))
    sql = 'SELECT * FROM links.links ORDER BY player_tag'
    with closing(sqlite3.connect(legacy)) as connection:
        connection.row_factory = sqlite3.Row
        before = [dict(row) for row in connection.execute('SELECT * FROM links ORDER BY player_tag')]
    sources = (replace(SOURCES[0], path=legacy),)
    result, _ = await asyncio.to_thread(run_query, sources, paths, set(), sql)
    compare('agent/SQL_links', before, result.get('rows', result))
    for table in ('suggestions', 'ignored_tags'):
        with closing(sqlite3.connect(legacy)) as connection:
            connection.row_factory = sqlite3.Row
            before = [dict(row) for row in connection.execute(f'SELECT * FROM {table}')]
        result, _ = await asyncio.to_thread(run_query, sources, paths, {'recruiter_or_core'}, f'SELECT * FROM links.{table}')
        compare('agent/' + table, before, result.get('rows', result))
    if await asyncio.to_thread(legacy.read_bytes) != old_bytes:
        raise AssertionError('Startup/readers changed the synthetic old links file')
    client.get.assert_not_awaited()
    checks = await store.call(lambda c: {'integrity': c.execute('PRAGMA integrity_check').fetchone()[0],
                                        'foreign_key_violations': len(c.execute('PRAGMA foreign_key_check').fetchall()),
                                        'links': c.execute('SELECT COUNT(*) FROM current_link_rows').fetchone()[0]})
    await service.close()
    await bot.close()
    return {'legacy_source': 'Synthetic six-column schema; no live links copy was supplied',
            'record_source': 'Read-only warsimport/_data copied into this worktree',
            'roster_replay': 'Stored clan payloads replayed offline as one fresh nine-clan generation',
            'rehearsal_setup': 'Cleared the existing people import gate in the local record copy only; retained dated history',
            'at': datetime.fromtimestamp(at / 1_000_000, timezone.utc).isoformat(),
            'comparison_count': len(comparisons), 'readers': comparisons, 'differences': differences,
            'old_file_unchanged': True, 'checks': checks}


if __name__ == '__main__':
    result = asyncio.run(rehearse())
    target = Path('_peoplefinish/rehearsal.json')
    target.write_text(json.dumps(result, indent=2, ensure_ascii=True, default=lambda value: value.__dict__), encoding='utf-8')
    print(json.dumps({key: value for key, value in result.items() if key != 'readers'}, indent=2,
                     default=lambda value: value.__dict__))
    raise SystemExit(bool(result['differences']))
