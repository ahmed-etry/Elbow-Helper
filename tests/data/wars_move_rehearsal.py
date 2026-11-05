"""Test-only rehearsal of the war features and the agent's war tools moving to the family record.

Three steps, each run from a source tree's root:

``python -m tests.data.wars_move_rehearsal prepare <data folder> <work folder>``
    Copies ``family/family.sqlite3``, ``clan_health/clan_health.db`` and
    ``war/war_cache.json`` into the work folder, runs the startup copy of the
    old wars into the record copy and fixes every selection both sides use.

``python -m tests.data.wars_move_rehearsal capture <work folder> <result file>``
    Runs in either tree. The old code (before the move) is driven by its own
    saved state: the old war cache's boards stand in for its last Clash
    answers, old clan health for stored wars and a war log rebuilt from the
    record copy for the monthly count. The new code reads the record copy.
    Discord, Clash and the LLM are mocked; nothing is sent anywhere.

``python -m tests.data.wars_move_rehearsal compare <work folder> <old result> <new result> <report file>``
    Explains every difference from the record copy; unexplained ones are listed.

The data folder is never written; each capture works on its own copies.
Results keep account tags but never member or account names.
"""

import asyncio
from collections import Counter
from contextlib import closing
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from elbow_helper.agent.reads import war_tools as war_reads
from elbow_helper.agent.models import AgentRequestContext
from elbow_helper.configuration.channels import CLAN_LEADERSHIP_CHANNELS, CLAN_WAR_CHANNELS
from elbow_helper.configuration.clans import CLANS, CLAN_LEADERSHIP_ROLE_IDS, CLAN_WAR_ROLE_IDS
from elbow_helper.configuration.roles import CORE
from elbow_helper.features.clan_health.database import ClanHealthRepository
from elbow_helper.features.cwl.queries import CwlQueries
from elbow_helper.features.wars import config
from elbow_helper.features.wars.board import WarBoardMixin
from elbow_helper.features.wars.emojis import EMPTY_WAR_EMOJIS
from elbow_helper.features.wars.helpers import HelperMixin, build_war_id
from elbow_helper.features.wars.queries import WarQueries
from elbow_helper.features.wars.rendering import build_war_board_embed, normalize_war_state
from elbow_helper.features.wars.roles import WarRoleMixin
from elbow_helper.features.wars.state import StateMixin
from elbow_helper.features.wars.tasks import TaskMixin
from elbow_helper.features.wars.warflow import WarflowMixin

NEW = importlib.util.find_spec("elbow_helper.features.wars.record") is not None
SECOND = 1_000_000
DAY = 86400
HELPER = HelperMixin()
CODES = {clan.tag: code for code, clan in CLANS.items()}
NAMES = {clan.name: code for code, clan in CLANS.items()}
WINDOWS = {"six_months": ("2026-05-01T00:00:00+00:00", "2026-11-01T00:00:00+00:00"),
           "feb_may": ("2026-02-01T00:00:00+00:00", "2026-06-01T00:00:00+00:00"),
           "aug_oct": ("2026-08-01T00:00:00+00:00", "2026-11-01T00:00:00+00:00")}
MONTHS = ("2026-08", "2026-09", "2026-10")
FILES = {"family.sqlite3": "family/family.sqlite3", "clan_health.db": "clan_health/clan_health.db",
         "war_cache.json": "war/war_cache.json"}


def seconds(text):
    return int(datetime.fromisoformat(text).timestamp())


def clash_time(value):
    moment = datetime.fromtimestamp(value // SECOND, timezone.utc)
    return f"{moment:%Y%m%dT%H%M%S}.{value % SECOND // 1000:03d}Z"


def digest(value):
    return hashlib.sha1(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()[:12]


def embed_digest(embed):
    payload = embed.to_dict()
    payload.pop("timestamp", None)
    return digest(payload)


def connect(path):
    connection = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


class Links:
    """Current account links from the record copy, in the account-link reader's shape."""

    def __init__(self, path):
        with closing(connect(path)) as connection:
            self.rows = {row["player_tag"]: int(row["discord_id"]) for row in connection.execute(
                "SELECT a.player_tag,m.discord_id FROM account_links l JOIN accounts a ON a.id=l.account_id "
                "JOIN members m ON m.id=l.member_id WHERE l.to_at IS NULL")}

    def get_all_links(self):
        return {tag: {"discord_user_id": member} for tag, member in self.rows.items()}

    def get_links_by_tags(self, tags):
        return {tag: ({"discord_user_id": self.rows[tag], "player_tag": tag} if tag in self.rows else None)
                for tag in tags}

    def get_links_for_members(self, ids):
        return {member: [{"player_tag": tag} for tag, owner in sorted(self.rows.items()) if owner == member]
                for member in ids}


# Prepare ---------------------------------------------------------------------------


def lineup_counts(connection, after, before):
    """Regular-war account-wars per account in family lineups, from the record copy."""
    tags = [clan.tag for clan in CLANS.values()]
    return dict(connection.execute(
        f"SELECT m.player_tag,COUNT(*) FROM war_members m JOIN wars w ON w.id=m.war_id JOIN war_sides s "
        f"ON s.war_id=w.id AND s.side=m.side WHERE w.type='REG' AND s.clan_tag IN ({','.join('?' * len(tags))}) "
        "AND w.end_at>=? AND w.end_at<? GROUP BY m.player_tag", (*tags, after * SECOND, before * SECOND)).fetchall())


def selections(work):
    """The scenarios both sides run, fixed once from the record copy and the old stored wars."""
    links = Links(work / "family.sqlite3")
    with closing(connect(work / "family.sqlite3")) as record, closing(connect(work / "clan_health.db")) as old:
        six, early, late = (tuple(map(seconds, WINDOWS[key])) for key in ("six_months", "feb_may", "aug_oct"))
        busy = lineup_counts(record, *six)
        by_member = Counter()
        for tag, count in busy.items():
            if tag in links.rows:
                by_member[links.rows[tag]] += count
        member = max(by_member, key=lambda key: (by_member[key], -key))
        cohort = sorted(busy, key=lambda tag: (-busy[tag], tag))[:150]
        earlier, later = lineup_counts(record, *early), lineup_counts(record, *late)
        quiet = sorted({links.rows[tag] for tag in earlier if tag in links.rows}
                       - {links.rows[tag] for tag in later if tag in links.rows})
        gone = sorted(set(earlier) - set(later))[:5]
        picked = [row[0] for row in old.execute(
            "SELECT war_id FROM wars WHERE war_type='REG' AND clan_code='BEC' AND state='warEnded' "
            "AND end_ts>=? AND end_ts<? ORDER BY end_ts DESC LIMIT 3", six)]
    window = dict(zip(("after", "before"), WINDOWS["six_months"]))
    return [
        {"name": "one member, regular, May-Oct", "arguments": {"war_type": "REG", "unit": "member",
                                                                 "member_ids": [member], **window}},
        {"name": "150 busiest accounts, regular, May-Oct", "arguments": {"war_type": "REG", "account_tags": cohort,
                                                                          **window}},
        {"name": "all clans, regular, May-Oct", "arguments": {"war_type": "REG", **window}},
        {"name": "all clans, CWL, May-Oct", "arguments": {"war_type": "CWL", **window}},
        {"name": "all clans, both, May-Oct", "arguments": {"war_type": "both", **window}},
        {"name": "all clans, regular, Feb-May", "arguments": {
            "war_type": "REG", **dict(zip(("after", "before"), WINDOWS["feb_may"]))}},
        {"name": "a member with zero wars, regular, Aug-Oct", "arguments": {
            "war_type": "REG", "unit": "member", "member_ids": quiet[:1],
            **dict(zip(("after", "before"), WINDOWS["aug_oct"]))}},
        {"name": "5 accounts with zero wars, regular, Aug-Oct", "arguments": {
            "war_type": "REG", "account_tags": gone, **dict(zip(("after", "before"), WINDOWS["aug_oct"]))}},
        {"name": "latest 10 regular wars, all clans", "arguments": {"war_type": "REG", "latest_completed": 10}},
        {"name": "latest 6 wars of both kinds, BEH", "arguments": {"war_type": "both", "latest_completed": 6,
                                                                    "clan_codes": ["BEH"]}},
        {"name": "follow-up by 3 old war IDs", "arguments": {"war_type": "REG", "war_ids": picked}},
    ]


async def prepare(data, work):
    from elbow_helper.data.store import FamilyStore
    from elbow_helper.data.store.wars_import import import_wars
    data, work = Path(data), Path(work)
    work.mkdir(parents=True, exist_ok=True)
    for name, source in FILES.items():
        shutil.copyfile(data / source, work / name)
    store = FamilyStore(work / "family.sqlite3")
    await store.initialize()
    imported = await store.call(import_wars, work / "clan_health.db", work / "war_cache.json")
    await store.call(lambda c: c.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall())
    at = await store.call(lambda c: c.execute(
        "SELECT MAX(last_attempt_at) FROM war_collection_state WHERE source_kind='current_war'").fetchone()[0]) + 5 * SECOND
    log_at = await store.call(lambda c: c.execute(
        "SELECT MAX(last_attempt_at) FROM war_collection_state WHERE source_kind='war_log'").fetchone()[0])
    prepared = {"at": at, "as_of": datetime.fromtimestamp(at / SECOND, timezone.utc).isoformat(),
                "last_war_log_check": datetime.fromtimestamp(log_at / SECOND, timezone.utc).isoformat(),
                "import": {"imported": imported["imported"], "wars": imported["checks"]["wars"]},
                "scenarios": selections(work)}
    (work / "prepared.json").write_text(json.dumps(prepared, indent=1, default=str), encoding="utf-8")
    return {key: value for key, value in prepared.items() if key != "scenarios"}


# Boards, summaries, roles and restarts ----------------------------------------------


class Discord:
    """Mocked channels, members and roles that record what the features would do."""

    def __init__(self, cache, state):
        self.events, self.roles_changed = [], []
        self.where = {channel: code for code, channel in CLAN_WAR_CHANNELS.items()}
        self.where.update({channel: f"leadership {code}" for code, channel in CLAN_LEADERSHIP_CHANNELS.items()})
        self.channels = {}
        for code, channel_id in CLAN_WAR_CHANNELS.items():
            self.channels[channel_id] = self.board(channel_id, code, cache, state.get("boards", {}).get(code))
        for channel_id in CLAN_LEADERSHIP_CHANNELS.values():
            self.channels.setdefault(channel_id, self.leadership(channel_id))
        self.role_ids = {role_id: MagicMock(id=role_id) for role_id in CLAN_WAR_ROLE_IDS.values()}
        self.members = {}
        self.held = state.get("held", {})

    def board(self, channel_id, code, cache, shown):
        """The board message as Discord shows it: the old feature's last render, or the previous run's."""
        channel = MagicMock(id=channel_id)
        message = MagicMock(id=channel_id * 10, channel=channel)
        if shown is None:
            history = cache.get("war_board_history", {}).get(code) or {}
            current, previous = history.get("current"), history.get("previous")
            embeds = [build_war_board_embed(current, EMPTY_WAR_EMOJIS)] if current else []
            button = (current is not None and normalize_war_state(current.get("state")) in {"preparation", "inwar"}
                      and previous is not None and normalize_war_state(previous.get("state")) == "warended")
            shown = (embeds, [f"war_board:previous:{code}"] if button else [])
        message.embeds = list(shown[0])
        message.components = [MagicMock(children=[MagicMock(custom_id=item) for item in shown[1]])] if shown[1] else []
        channel.fetch_message = AsyncMock(return_value=message)
        channel.message = message

        async def send(**kwargs):
            self.events.append({"where": code, "kind": "send", "embed": embed_digest(kwargs["embed"])})
            message.embeds = [kwargs["embed"]]
            return message
        channel.send = AsyncMock(side_effect=send)
        return channel

    def leadership(self, channel_id):
        channel = MagicMock(id=channel_id)

        async def send(**kwargs):
            embed = kwargs["embed"]
            missed = [field.value for field in embed.fields if field.name.startswith("Missed Attacks")]
            self.events.append({"where": self.where[channel_id], "kind": "summary", "title": embed.title,
                                "ended": embed.timestamp.isoformat() if embed.timestamp else None,
                                "embed": embed_digest(embed), "missed_lines": sum(
                                    len(value.splitlines()) for value in missed if value != "None")})
            sent = MagicMock(id=len(self.events) + 5000, channel=channel, created_at=datetime.now(timezone.utc))
            sent.author.id = 99
            return sent
        channel.send = AsyncMock(side_effect=send)
        channel.fetch_message = AsyncMock(return_value=MagicMock(delete=AsyncMock()))
        channel.history.side_effect = lambda **kwargs: _empty()
        return channel

    async def edit(self, message, **kwargs):
        view = kwargs.get("view")
        self.events.append({"where": self.where.get(message.channel.id, "?"), "kind": "edit",
                            "embed": embed_digest(kwargs["embed"]) if kwargs.get("embed") else None,
                            "view": type(view).__name__ if view is not None else None})
        if kwargs.get("embed") is not None:
            message.embeds = [kwargs["embed"]]
        message.components = ([MagicMock(children=[MagicMock(custom_id=item.custom_id) for item in view.children])]
                               if view is not None else [])
        return MagicMock(error=None)

    def member(self, member_id):
        if member_id not in self.members:
            held = [self.role_ids[CLAN_WAR_ROLE_IDS[code]] for code, ids in self.held.items()
                    if member_id in ids and code in CLAN_WAR_ROLE_IDS]
            target = MagicMock(id=member_id, roles=held)
            target.add_roles = AsyncMock(side_effect=lambda role, reason, m=member_id: self.roles_changed.append(
                ("add", role.id, m)))
            target.remove_roles = AsyncMock(side_effect=lambda role, reason, m=member_id: self.roles_changed.append(
                ("remove", role.id, m)))
            self.members[member_id] = target
        return self.members[member_id]

    def shown(self):
        return {code: ([*self.channels[channel].message.embeds],
                       [child.custom_id for row in self.channels[channel].message.components for child in row.children])
                for code, channel in CLAN_WAR_CHANNELS.items()}

    def role_counts(self):
        by_role = {role_id: code for code, role_id in CLAN_WAR_ROLE_IDS.items()}
        return dict(Counter(f"{action} {by_role[role]}" for action, role, _ in self.roles_changed))


async def _empty():
    for item in ():
        yield item


def manager_for(work, cache, links, discord_, state, at):
    from elbow_helper.data.reads import WarReads
    from elbow_helper.data.store import FamilyStore
    if NEW:
        from elbow_helper.features.wars.record import RecordMixin as Source
    else:
        from elbow_helper.features.wars.api import ApiMixin as Source
    Manager = type("Manager", (StateMixin, Source, HelperMixin, WarBoardMixin, WarRoleMixin, WarflowMixin, TaskMixin), {})
    manager = Manager()
    manager.cache = deepcopy(cache)
    manager._save_cache_async = AsyncMock()
    manager.war_emojis = MagicMock(get=AsyncMock(return_value=EMPTY_WAR_EMOJIS))
    manager.war_board_registry = {code: dict(entry) for code, entry in cache.get("war_board_messages", {}).items()}
    roles = state.get("roles", cache.get("war_role_state", {}))
    manager.war_role_lineups = {code: dict(entry.get("lineup", {})) for code, entry in roles.items() if entry.get("lineup")}
    manager.war_role_managed_members = {code: set(entry.get("managed", [])) for code, entry in roles.items()
                                        if entry.get("managed")}
    discord_.held = {code: set(ids) for code, ids in manager.war_role_managed_members.items()}
    manager._war_role_locks, manager._war_role_missing_links = {}, {}
    # Offline, no roster signup is known; a removal is planned, not certain.
    manager._roster_role_claim = AsyncMock(return_value=False)
    manager.account_links = links
    manager.summary_registry = {key: value for key, value in cache.get("summary_messages", {}).items()
                                if isinstance(value, dict)}
    processed = state.get("processed", [war for war in cache.get("processed_wars", []) if isinstance(war, str)])
    manager.processed_war_order, manager.processed_war_ids = list(processed), set(processed)
    manager.war_context, manager._war_state_locks, manager._wars_in_flight = {}, {}, {}
    manager._war_summary_state_lock = asyncio.Lock()
    manager.clan_channels = {clan.name: {key: value for key, value in (
        ("leadership_channel", CLAN_LEADERSHIP_CHANNELS.get(code)),
        ("leadership_role", CLAN_LEADERSHIP_ROLE_IDS.get(code))) if value is not None} for code, clan in CLANS.items()}
    manager._startup_sync_done = asyncio.Event()
    manager.war_observations = {}
    if NEW:
        store = FamilyStore(work / "family.sqlite3")
        manager.war_reads = WarReads(store)
        manager.record_boards, manager._record_checked, manager._record_views = {}, {}, set()
        manager._record_lock = asyncio.Lock()
    else:
        # The old feature's saved boards stand in for its last Clash answers.
        manager.war_board_history = deepcopy(state.get("history", cache.get("war_board_history", {})))
        answers = {clan.name: (cache.get("war_board_history", {}).get(code) or {}).get("current")
                   for code, clan in CLANS.items()}
        manager.clash_client = SimpleNamespace(configured=True)
        manager._war_fetch_warning_state = {}
        manager._fetch_current_war = AsyncMock(side_effect=lambda clan: deepcopy(answers.get(clan)))
        manager._fetch_war_log_result = AsyncMock(return_value=None)
    guild = MagicMock()
    guild.get_role.side_effect = discord_.role_ids.get
    guild.get_member.side_effect = discord_.member
    manager.bot = MagicMock()
    manager.bot.publishing.edit_message = AsyncMock(side_effect=discord_.edit)
    manager.bot.user.id = 99
    manager.bot.wait_until_ready = AsyncMock()
    manager.bot.get_guild.return_value = guild
    manager.bot.get_channel.side_effect = discord_.channels.get
    if not NEW:
        # The old feature restored its board buttons from the saved boards when it loaded.
        manager._register_war_board_views()
    return manager


async def one_pass(manager):
    """What the old poll or a collection pass does once."""
    if NEW:
        await manager._refresh_from_record()
        return

    async def stop_after_the_pass(delay):
        if delay >= 300:
            raise asyncio.CancelledError
    with patch("elbow_helper.features.wars.tasks.asyncio.sleep", side_effect=stop_after_the_pass):
        await manager._poll_coc_api()


async def startup(manager):
    if NEW:
        await manager._sync_war_state_from_record()
    else:
        with patch("elbow_helper.features.wars.tasks.asyncio.sleep", AsyncMock()):
            await manager._sync_war_state_on_startup()
        await one_pass(manager)


def snapshot_facts(snapshot):
    return {"evidence_status": snapshot.evidence_status, "war_id": snapshot.war_id, "state": snapshot.state,
            "result": snapshot.result, "team_size": snapshot.team_size, "members": len(snapshot.members),
            "clan_stars": snapshot.clan_stars, "clan_destruction": snapshot.clan_destruction,
            "opponent_stars": snapshot.opponent_stars, "missed": sum(row.missed_attacks for row in snapshot.members),
            "used": sum(row.attacks_used for row in snapshot.members), "roster_complete": snapshot.roster_complete}


def board_state(manager, discord_):
    queries = WarQueries(manager)
    boards = {}
    for code, (embeds, controls) in discord_.shown().items():
        previous = manager._previous_war_snapshot(code)
        status = queries.status(code)
        snapshots = {}
        for slot in ("current", "previous"):
            try:
                snapshots[slot] = snapshot_facts(queries.snapshot(code, selected=slot))
            except ValueError as error:
                snapshots[slot] = {"error": str(error)}
        boards[code] = {
            "board": embed_digest(embeds[0]) if embeds else None, "controls": controls,
            "previous_war_id": build_war_id(previous) if previous else None,
            "previous_render": embed_digest(build_war_board_embed(previous, EMPTY_WAR_EMOJIS)) if previous else None,
            "status": {"evidence_status": status.evidence_status, "current_war_id": status.current_war_id,
                       "current_state": status.current_state, "previous_war_id": status.previous_war_id},
            "snapshots": snapshots}
    return boards


def saved_state(manager, discord_):
    roles = {code: {"lineup": manager.war_role_lineups.get(code, {}),
                    "managed": sorted(manager.war_role_managed_members.get(code, ()))}
             for code in set(manager.war_role_lineups) | set(manager.war_role_managed_members)}
    state = {"processed": list(manager.processed_war_order), "roles": roles, "boards": discord_.shown()}
    if not NEW:
        state["history"] = deepcopy(manager.war_board_history)
    return state


def run_summary(manager, discord_, processed_before):
    return {"events": discord_.events, "summaries": [event for event in discord_.events if event["kind"] == "summary"],
            "roles": discord_.role_counts(),
            "processed_added": sorted(set(manager.processed_war_order) - set(processed_before)),
            "claims": {code: len(set(lineup.values())) for code, lineup in sorted(manager.war_role_lineups.items())}}


async def features(work, prepared, links):
    cache = json.loads((work / "war_cache.json").read_text(encoding="utf-8"))
    at = prepared["at"]
    runs, state, manager = {}, {}, None
    with patch("elbow_helper.data.reads.core.now_us", return_value=at):
        for name in ("startup", "next pass", "second restart"):
            discord_ = Discord(cache, state)
            if name == "next pass":
                # The same running feature, after one more poll or collection pass.
                manager.bot.get_channel.side_effect = discord_.channels.get
                manager.bot.publishing.edit_message = AsyncMock(side_effect=discord_.edit)
                guild = manager.bot.get_guild.return_value
                guild.get_role.side_effect, guild.get_member.side_effect = discord_.role_ids.get, discord_.member
                discord_.held = {code: set(ids) for code, ids in manager.war_role_managed_members.items()}
                before = list(manager.processed_war_order)
                await one_pass(manager)
            else:
                manager = manager_for(work, cache, links, discord_, state, at)
                before = list(manager.processed_war_order)
                await startup(manager)
            runs[name] = run_summary(manager, discord_, before)
            runs[name]["boards"] = board_state(manager, discord_)
            runs[name]["views_registered"] = sorted(call.kwargs.get("message_id") for call in
                                                    manager.bot.add_view.call_args_list)
            state = saved_state(manager, discord_)
    return runs


async def by_identity(work, cache, at):
    """New tree only: each old board war against the same record war, rendered and field by field."""
    from elbow_helper.data.reads.wars import war_view
    from elbow_helper.features.wars.record import board_payload
    rows = []
    with closing(sqlite3.connect(work / "family.sqlite3")) as connection:
        connection.row_factory = sqlite3.Row
        for code in config.WAR_BOARD_CLAN_CODES:
            for slot in ("current", "previous"):
                old = (cache.get("war_board_history", {}).get(code) or {}).get(slot)
                if not isinstance(old, dict):
                    continue
                found = connection.execute("SELECT war_id FROM war_legacy_ids WHERE source='war_cache' AND legacy_id=? "
                                           "AND clan_code=?", (build_war_id(old), code)).fetchone()
                if found is None:
                    rows.append({"clan_code": code, "slot": slot, "record_war": None})
                    continue
                war = connection.execute("SELECT * FROM wars WHERE id=?", (found[0],)).fetchone()
                view = war_view(connection, war, CLANS[code].tag, at, True)
                payload = board_payload(view)
                own = view["coverage"]["own"]
                rows.append({"clan_code": code, "slot": slot, "record_war": view["war_id"],
                             "record_type": f"{view['type']}/{view['type_basis']}",
                             "old_state": old["state"], "record_state": view["state"],
                             "old_end": old.get("endTime"), "record_end": payload.get("endTime"),
                             "same_render": embed_digest(build_war_board_embed(old, EMPTY_WAR_EMOJIS))
                             == embed_digest(build_war_board_embed(payload, EMPTY_WAR_EMOJIS)),
                             "same_misses": sorted(HELPER._compute_missed(old)) == sorted(HELPER._compute_missed(payload)),
                             "old_attacks": (old.get("clan") or {}).get("attacks"),
                             "record_attacks": (payload.get("clan") or {}).get("attacks"),
                             "attacks_reported": own["attacks_reported"], "attacks_stored": own["attacks_stored"],
                             "old_stars": (old.get("clan") or {}).get("stars"),
                             "record_stars": (payload.get("clan") or {}).get("stars")})
    return rows


# Monthly war summaries ----------------------------------------------------------------


def rebuilt_log(connection, clan, at):
    """The clan's war log as Clash lists it, newest first, rebuilt from the record's log rows."""
    entries = [(row[0], row[1]) for row in connection.execute(
        "SELECT w.end_at,s.result FROM wars w JOIN war_sides s ON s.war_id=w.id AND s.clan_tag=? "
        "WHERE w.end_at<=? AND EXISTS (SELECT 1 FROM war_source_keys k WHERE k.war_id=w.id AND k.source_kind='war_log')",
        (clan, at))]
    entries += [(row[0], row[1]) for row in connection.execute(
        "SELECT end_at,result FROM cwl_log_summaries WHERE clan_tag=? AND end_at<=?", (clan, at))]
    entries.sort(key=lambda entry: entry[0], reverse=True)
    return [{"endTime": clash_time(end), "result": result} for end, result in entries]


async def monthly(work, prepared):
    from elbow_helper.features.clan_reporting import wars as reporting
    from elbow_helper.data.reads import WarReads
    from elbow_helper.data.store import FamilyStore
    if NEW:
        Reporter = type("Reporter", (reporting.ClanReportingWarMixin,), {})
    else:
        from elbow_helper.features.clan_reporting.api import ClanReportingApiMixin
        Reporter = type("Reporter", (reporting.ClanReportingWarMixin, ClanReportingApiMixin), {})
    at = prepared["at"]
    logs = {}
    with closing(sqlite3.connect(work / "family.sqlite3")) as connection:
        for clan in CLANS.values():
            logs[clan.tag] = rebuilt_log(connection, clan.tag, at)
    rows = []
    charts = []
    with patch.object(reporting, "build_war_summary_chart", side_effect=lambda *args: charts.append(args) or object()), \
            patch.object(reporting, "save_state"), \
            patch("elbow_helper.data.reads.core.now_us", return_value=at):
        for month in MONTHS:
            start = datetime.strptime(month, "%Y-%m").replace(tzinfo=timezone.utc)
            end = start.replace(year=start.year + (start.month == 12), month=start.month % 12 + 1)
            for code, clan in CLANS.items():
                reporter = Reporter()
                reporter.state = {}
                reporter.bot = SimpleNamespace(wait_until_ready=AsyncMock())
                sent = []
                thread = SimpleNamespace(send=AsyncMock(side_effect=lambda **kwargs: sent.append(kwargs) or SimpleNamespace(id=1)))
                reporter._get_thread = AsyncMock(return_value=thread)
                reporter._fetch_message = AsyncMock(return_value=None)
                if NEW:
                    reporter.war_reads = WarReads(FamilyStore(work / "family.sqlite3"))
                else:
                    reporter.clash_client = SimpleNamespace(configured=True)
                    reporter._fetch_war_log = AsyncMock(side_effect=lambda tag, limit=50: logs[tag][:limit])
                charts.clear()
                completed = await reporter._post_war_summary_for_clan(code, start, end)
                posted = ("chart", list(charts[0][2:])) if charts and sent else (
                    ("notice", sent[0]["embed"].description) if sent else ("nothing", None))
                row = {"clan": code, "month": month, "completed": completed, "posted": posted[0],
                       "counts": posted[1] if posted[0] == "chart" else None,
                       "notice": posted[1] if posted[0] == "notice" else None}
                if NEW:
                    # What the record counts for the month, posted or not.
                    from elbow_helper.features.clan_reporting.record_wars import monthly_results
                    counted = await monthly_results(reporter.war_reads, clan.tag, month)
                    row["record"] = {"status": counted["status"], "read_after_month": counted["coverage"]["read_after_month"],
                                     "counts": [counted["wins"], counted["losses"], counted["ties"]]}
                rows.append(row)
    return rows


# Agent war tools ---------------------------------------------------------------------


def base_context():
    actor = SimpleNamespace(id=41, display_name="Rehearsal", roles=[SimpleNamespace(id=next(iter(CORE)))])
    channel = SimpleNamespace(id=91, type=discord.ChannelType.text, overwrites={},
                              permissions_for=lambda _: SimpleNamespace(view_channel=True, read_message_history=True))
    role = SimpleNamespace(id=1)
    guild = SimpleNamespace(id=3, name="Rehearsal", me=SimpleNamespace(id=999, bot=True, roles=[]), chunked=True,
                            members=[actor], roles=[role, *actor.roles], default_role=role,
                            get_member=lambda _: None, get_channel_or_thread=lambda _: channel, channels=(channel,))
    channel.guild = guild
    message = SimpleNamespace(id=71, channel=channel, created_at=datetime(2026, 11, 1, tzinfo=timezone.utc))
    return AgentRequestContext(bot=None, guild=guild, member=actor, source_message=message,
                               account_links=None, message_search=None)


def summarize(result):
    """Per result row and war: what is compared, without names."""
    rows = {}
    for row in result.get("rows", []):
        key = str(row.get("member_id")) if row.get("unit") == "member" and row.get("member_id") else row.get("player_tag")
        rows[key] = {"wars": len(row["war_ids"]), "war_ids": sorted(row["war_ids"]), "attacks_used": row["attacks_used"],
                     "attacks_expected": row["attacks_expected"], "missed": row["missed"],
                     "unconfirmed": row.get("unconfirmed_opportunities"), "status": row.get("participation_status"),
                     "observation": row.get("observation_status"),
                     "stars": row["attack_performance"].get("stars"), "triples": row["attack_performance"].get("triples"),
                     "observed_attacks": row["attack_performance"].get("observed_attacks")}
    return {"error": result.get("error"), "cause": result.get("cause"), "rows": rows,
            "war_ids": result.get("war_ids", []), "wars_without_rosters": result.get("wars_without_rosters", []),
            "clan_totals": result.get("clan_totals"),
            "coverage": {key: value for key, value in (result.get("coverage") or {}).items()
                         if key not in ("source_status", "regular", "league")}}


def source_rows(source):
    fields = ("attacks_used", "attacks_expected", "missed", "observed_attack_performance", "clan_code")
    return {"wars": [{key: war.get(key) for key in ("war_id", "clan_code", "aliases", "type_basis", "end_ts")}
                     for war in source.get("war_aliases", [])],
            "rows": {f"{row['war_id']} {row['player_tag']}": {field: row.get(field) for field in fields}
                     for row in source.get("rows", [])}}


async def agent(work, prepared, links, cache):
    from elbow_helper.data.reads import WarReads
    from elbow_helper.data.store import FamilyStore
    context = replace(base_context(), account_links=links,
                      cwl_queries=CwlQueries(ClanHealthRepository(work / "clan_health.db"), lambda: {}))
    if NEW:
        context = replace(context, bot=SimpleNamespace(war_reads=WarReads(FamilyStore(work / "family.sqlite3"))))
        from elbow_helper.data.reads import CwlReads
        from elbow_helper.features.cwl.record import CwlHistory
        context = replace(context, cwl_queries=CwlQueries(
            CwlHistory(CwlReads(FamilyStore(work / "family.sqlite3")),
                       ClanHealthRepository(work / "clan_health.db")), lambda: {}))
    boards = {code: entry.get("current") for code, entry in cache.get("war_board_history", {}).items()}

    async def clash(context, arguments):
        if NEW:
            raise AssertionError("the new war tools make no Clash request")
        # The old current-war tool read Clash; the old feature's saved boards stand in for it.
        return {"items": [{"tag": tag, "status": "ok", "data": deepcopy(boards[CODES[tag]])} if boards.get(CODES[tag])
                          else {"tag": tag, "status": "unavailable"} for tag in arguments["tags"]]}

    results, timings = [], []
    with patch.object(war_reads, "read_clash", AsyncMock(side_effect=clash)), \
            patch("elbow_helper.data.reads.core.now_us", return_value=prepared["at"]):
        for scenario in prepared["scenarios"]:
            arguments = scenario["arguments"]
            local = replace(context, state=type(context.state)())
            started = time.perf_counter()
            result = await war_reads.attendance(local, arguments)
            timings.append(round(time.perf_counter() - started, 3))
            selection = {key: value for key, value in arguments.items()
                         if key not in ("unit", "member_ids", "account_tags", "sort", "top_n")}
            source = await war_reads.selected_wars(replace(context, state=type(context.state)()), selection,
                                                   arguments["war_type"])
            results.append({"name": scenario["name"], "seconds": timings[-1], **summarize(result),
                            "source": source_rows(source)})
        current = await war_reads.current_war(replace(context, state=type(context.state)()), {})
    current_rows = [{"clan_code": row["clan_code"], "status": row["status"], "state": row["state"],
                     "end_at": row["end_at"], "start_at": row["start_at"], "team_size": row["team_size"],
                     "score": row["score"], "observed_at": row.get("observed_at"),
                     "members": {member["player_tag"]: {key: member[key] for key in ("attacks_used", "unused_attacks", "missed")}
                                 for member in row["members"]}}
                    for row in current["rows"]]
    return {"scenarios": results, "current_war": current_rows, "coverage_note": current.get("coverage")}


async def capture(work, out):
    work = Path(work)
    prepared = json.loads((work / "prepared.json").read_text(encoding="utf-8"))
    side = work / ("new" if NEW else "old")
    side.mkdir(exist_ok=True)
    for name in FILES:
        shutil.copyfile(work / name, side / name)
    links = Links(side / "family.sqlite3")
    cache = json.loads((side / "war_cache.json").read_text(encoding="utf-8"))
    result = {"side": "new" if NEW else "old", "as_of": prepared["as_of"],
              "features": await features(side, prepared, links),
              "monthly": await monthly(side, prepared),
              "agent": await agent(side, prepared, links, cache)}
    if NEW:
        result["by_identity"] = await by_identity(side, cache, prepared["at"])
    Path(out).write_text(json.dumps(result, indent=1, sort_keys=True, default=str), encoding="utf-8")
    return {"side": result["side"], "written": str(out)}


# Compare -----------------------------------------------------------------------------


class Record:
    """Reasons for differences, read from the prepared record copy."""

    def __init__(self, work):
        self.connection = connect(Path(work) / "family.sqlite3")

    def legacy(self, legacy_id, code, source=None):
        return self.connection.execute(
            "SELECT w.*,l.capture_state FROM war_legacy_ids l JOIN wars w ON w.id=l.war_id WHERE l.legacy_id=? "
            f"AND l.clan_code=?{' AND l.source=?' if source else ''}",
            (legacy_id, code, *((source,) if source else ()))).fetchone()

    def side(self, war_id, code):
        side = self.connection.execute("SELECT * FROM war_sides WHERE war_id=? AND clan_tag=?",
                                       (war_id, CLANS[code].tag)).fetchone()
        counts = self.connection.execute("SELECT COUNT(*),SUM(attacks_used) FROM war_members WHERE war_id=? AND side=?",
                                         (war_id, side["side"])).fetchone()
        return side, counts

    def states(self, aliases, code):
        return {row[0] for alias in aliases for row in self.connection.execute(
            "SELECT capture_state FROM war_legacy_ids WHERE legacy_id=? AND clan_code=?", (alias, code))}


def compare_features(old, new, identity):
    """Per run and board: the same final state, or the reason it differs."""
    by_war = {(row["clan_code"], row["slot"]): row for row in identity}
    findings = []
    for name in ("startup", "next pass", "second restart"):
        a, b = old["features"][name], new["features"][name]
        findings.append({"run": name, "summaries": {"old": len(a["summaries"]), "new": len(b["summaries"])},
                         "summary_detail": {"old": a["summaries"], "new": b["summaries"]},
                         "roles": {"old": a["roles"], "new": b["roles"]}, "claims": {"old": a["claims"], "new": b["claims"]},
                         "board_events": {"old": [e for e in a["events"] if e["kind"] != "summary"],
                                          "new": [e for e in b["events"] if e["kind"] != "summary"]},
                         "views_registered": {"old": a["views_registered"], "new": b["views_registered"]}})
    boards = []
    for code in config.WAR_BOARD_CLAN_CODES:
        a, b = old["features"]["startup"]["boards"][code], new["features"]["startup"]["boards"][code]
        current, previous = by_war.get((code, "current")), by_war.get((code, "previous"))
        entry = {"clan_code": code, "same_board": a["board"] == b["board"], "same_controls": a["controls"] == b["controls"],
                 "same_previous": a["previous_war_id"] == b["previous_war_id"],
                 "same_previous_render": a["previous_render"] == b["previous_render"],
                 "status": {"old": a["status"], "new": b["status"]}, "controls": {"old": a["controls"], "new": b["controls"]}}
        reasons = []
        if b["status"]["evidence_status"] == "not_in_war" and a["status"]["evidence_status"] == "observed":
            reasons.append("the old stand-in Clash answer is the board's last war; the record's check says the clan "
                           "is not in war, so the queries say not in war and the board keeps its last war")
        if not entry["same_board"]:
            if current and current["record_state"] != current["old_state"]:
                reasons.append(f"capture time: the old cache saw this war {current['old_state']}, the record copy "
                               f"last saw it {current['record_state']}")
            elif current and current["same_render"] is False:
                reasons.append("capture time: two mid-war captures taken at different times")
            elif b["status"]["evidence_status"] == "not_in_war":
                reasons.append("the record's check says the clan is not in war; the board keeps its last war, "
                               "as the old poll did")
            else:
                reasons.append("unexplained")
        old_current = (a["status"]["current_war_id"] or "").split("-")
        new_previous = (b["previous_war_id"] or "").split("-")
        if (b["status"]["evidence_status"] == "not_in_war" and len(old_current) == 3 == len(new_previous)
                and (old_current[0], old_current[2]) == (new_previous[0], new_previous[2])):
            # Same preparation start and opponent: the same war, its end a second or two apart at most.
            reasons.append("not in war: the record's previous war is the board's last war; the old code moved it to "
                           "previous only when the next war started, and the button shows only during a war")
        elif not entry["same_previous"] or not entry["same_previous_render"]:
            if previous and previous["old_end"] != previous["record_end"] and "war_log" in previous["record_type"]:
                reasons.append("previous war learned from the war log first: the record keeps the log's end, a second "
                               "or two later, so its war ID differs; the old ID is kept as an alias")
            if previous and previous["old_attacks"] != previous["record_attacks"]:
                reasons.append(f"previous war: Clash's war log counts {previous['attacks_reported']} attacks, the old "
                               f"final capture {previous['attacks_stored']}")
            if previous is None and b["previous_war_id"] and a["previous_war_id"]:
                reasons.append("unexplained")
            if not reasons and current and current["record_state"] != current["old_state"]:
                reasons.append("capture time: the record copy's current war is an earlier one, so its previous war "
                               "is the war before")
            if not reasons:
                reasons.append("unexplained")
        if a["status"] != b["status"] and not reasons:
            reasons.append("unexplained")
        entry["reasons"] = reasons
        boards.append(entry)
    return {"runs": findings, "boards": boards, "by_identity": identity}


def why_only_old(record, war_id, code, window):
    war = record.legacy(war_id, code, "clan_health")
    if war is None:
        return "not copied into the record"
    if war["type"] == "FRIENDLY":
        return "friendly war: the record proves it from its times; old regular wars included friendlies"
    if war["state"] != "warEnded":
        return "record last saw it before the end"
    if window and not window[0] * SECOND <= war["end_at"] < window[1] * SECOND:
        return "record keeps a different end that falls outside the window"
    return "unexplained"


def why_only_new(record, war, code):
    states = record.states(war["aliases"], code)
    if not states:
        return ("known only from Clash's war log; old data never captured it: no lineup, so absences there are unknown"
                if war.get("type_basis") == "war_log" else "collected only by the record")
    if "warEnded" not in states:
        return ("old data last captured it mid-war, so old reads left it out; the record learned its end from the war "
                "log and shows it, misses unknown unless an account used every attack")
    return "unexplained"


def why_row(record, old_id, code, old, new):
    war = record.legacy(old_id, code, "clan_health")
    if war is None:
        return "unexplained"
    side, counts = record.side(war["id"], code)
    reasons = []
    if old["missed"] is not None and new["missed"] is None:
        if war["state"] != "warEnded":
            reasons.append("record last saw it before the end")
        elif side["attacks"] is not None and counts[1] != side["attacks"]:
            reasons.append("Clash's war log counts more attacks than the old final capture's accounts add up to; an "
                           "account short of its attacks may have made it")
        else:
            reasons.append("unexplained")
    if old["attacks_used"] != new["attacks_used"]:
        reasons.append("old counts the stored attack list, the record the account's attack count"
                       if old["attacks_used"] == old["observed_attack_performance"]["attacks"] else "unexplained")
    if old["observed_attack_performance"] != new["observed_attack_performance"] and not reasons:
        reasons.append("unexplained")
    if old["attacks_expected"] != new["attacks_expected"] and not reasons:
        reasons.append("unexplained")
    return "; ".join(reasons) or "unexplained"


def compare_scenario(record, old, new, window):
    old_wars = {war["war_id"]: war for war in old["source"]["wars"]}
    new_wars = {war["war_id"]: war for war in new["source"]["wars"]}
    by_alias = {alias: war_id for war_id, war in new_wars.items() for alias in war.get("aliases") or [war_id]}
    matched, only_old, only_new = {}, [], []
    for war_id, war in old_wars.items():
        hit = next((by_alias[alias] for alias in (war.get("aliases") or [war_id]) if alias in by_alias), None)
        if hit is None:
            only_old.append({"war_id": war_id, "clan_code": war["clan_code"],
                             "reason": why_only_old(record, war_id, war["clan_code"], window)
                             if war_id.startswith("REG") else "unexplained"})
        else:
            matched[war_id] = hit
    for war_id, war in new_wars.items():
        if war_id not in matched.values():
            only_new.append({"war_id": war_id, "clan_code": war["clan_code"],
                             "reason": why_only_new(record, war, war["clan_code"])})
    rows, compared = [], ("attacks_used", "attacks_expected", "missed", "observed_attack_performance")
    old_rows = {tuple(key.split(" ")): value for key, value in old["source"]["rows"].items()}
    new_rows = {tuple(key.split(" ")): value for key, value in new["source"]["rows"].items()}
    for (war_id, tag), row in old_rows.items():
        if war_id not in matched:
            continue
        other = new_rows.get((matched[war_id], tag))
        if other is None:
            rows.append({"war_id": war_id, "player_tag": tag, "reason": "account not in the record's lineup"})
        elif any(row[field] != other[field] for field in compared):
            rows.append({"war_id": war_id, "player_tag": tag, "reason": why_row(record, war_id, row["clan_code"], row, other)})
    back = {new_id: old_id for old_id, new_id in matched.items()}
    for war_id, tag in new_rows:
        if war_id in back and (back[war_id], tag) not in old_rows:
            rows.append({"war_id": war_id, "player_tag": tag, "reason": "account only in the record's lineup"})
    reasons = Counter(item["reason"] for item in only_old + only_new + rows)
    # Each differing result row is explained by the differences of the wars it covers.
    per_war = {}
    for item in only_old + only_new:
        per_war.setdefault(item["war_id"], set()).add(item["reason"])
    for item in rows:
        per_war.setdefault(item["war_id"], set()).add(item["reason"])
    for old_id, new_id in matched.items():
        if old_id in per_war:
            per_war.setdefault(new_id, set()).update(per_war[old_id])
    accounts = {}
    for key in sorted(old["rows"].keys() | new["rows"].keys()):
        a, b = old["rows"].get(key), new["rows"].get(key)
        if a == b:
            continue
        a_fields = {k: v for k, v in (a or {}).items() if k != "war_ids"}
        b_fields = {k: v for k, v in (b or {}).items() if k != "war_ids"}
        mapped = sorted(matched.get(war, war) for war in (a or {}).get("war_ids", []))
        if a_fields == b_fields and mapped == (b or {}).get("war_ids", []):
            accounts[key] = {"same war IDs once old IDs are matched to the record's"}
            continue
        wars = set((a or {}).get("war_ids", [])) | set((b or {}).get("war_ids", []))
        found = set().union(*(per_war.get(war, set()) for war in wars)) if wars else set()
        if not found and new.get("wars_without_rosters") and (b or {}).get("observation") == "no_stored_participation":
            found = {"the record has wars without a stored lineup in the window, so an account in no lineup is not "
                     "a known zero"}
        accounts[key] = found or {"unexplained"}
    explained = Counter(reason for found in accounts.values() for reason in found)
    same = sum(1 for key in old["rows"].keys() & new["rows"].keys() if old["rows"][key] == new["rows"][key])
    return {"wars": {"old": len(old_wars), "new": len(new_wars), "matched": len(matched)},
            "rows": {"old": len(old["rows"]), "new": len(new["rows"]), "same": same, "differing": len(accounts),
                     "same_once_ids_matched": sum(1 for found in accounts.values()
                                                  if found == {"same war IDs once old IDs are matched to the record's"})},
            "account_reasons": dict(explained.most_common()), "war_reasons": dict(reasons.most_common()),
            "only_old": only_old, "only_new": only_new,
            "unexplained": sorted({key for key, found in accounts.items() if "unexplained" in found})[:20]
            + [item for item in only_old + only_new + rows if "unexplained" in item["reason"]][:20]}


def compare_agent(record, old, new, scenarios):
    results = []
    for scenario, a, b in zip(scenarios, old["agent"]["scenarios"], new["agent"]["scenarios"]):
        arguments = scenario["arguments"]
        window = (seconds(arguments["after"]), seconds(arguments["before"])) if arguments.get("after") else None
        entry = {"name": scenario["name"], "errors": [a["error"], b["error"]], "seconds": [a["seconds"], b["seconds"]],
                 "coverage": {"old": a["coverage"], "new": b["coverage"]},
                 "wars_without_lineup": [len(a["wars_without_rosters"]), len(b["wars_without_rosters"])],
                 "totals": {side: {field: sum((row[field] or 0) for row in result["rows"].values())
                                   for field in ("wars", "attacks_used", "attacks_expected", "missed", "unconfirmed",
                                                 "stars", "triples")} for side, result in (("old", a), ("new", b))},
                 "zero_rows": {side: [{k: v for k, v in row.items() if k not in ("war_ids",)}
                                      for row in result["rows"].values() if not row["wars"]]
                               for side, result in (("old", a), ("new", b))},
                 **compare_scenario(record, a, b, window)}
        results.append(entry)
    current = []
    for a, b in zip(old["agent"]["current_war"], new["agent"]["current_war"]):
        same_war = (a["end_at"], a["start_at"]) == (b["end_at"], b["start_at"]) and a["state"] is not None
        same = same_war and a["state"] == b["state"] and a["score"] == b["score"] and a["members"] == b["members"]
        reason = (None if same else
                  "capture time: the old cache's board is hours newer than the record copy" if same_war else
                  "the old stand-in is the board's last war; the record's check says the clan is not in war"
                  if a["state"] == "warEnded" and b["state"] == "notInWar" else
                  "no saved board for this clan; the record's check says the clan is not in war"
                  if a["status"] == "unavailable" and b["state"] == "notInWar" else "unexplained")
        current.append({"clan_code": a["clan_code"], "old": [a["status"], a["state"], a["end_at"]],
                        "new": [b["status"], b["state"], b["end_at"], b["observed_at"]], "same_war": same_war,
                        "score_same": a["score"] == b["score"], "members_same": a["members"] == b["members"],
                        "reason": reason})
    return {"scenarios": results, "current_war": current}


def compare_monthly(old, new):
    rows = []
    for a, b in zip(old["monthly"], new["monthly"]):
        same = (a["posted"], a["counts"], a["notice"], a["completed"]) == (b["posted"], b["counts"], b["notice"], b["completed"])
        reason = None
        # An empty month posts a notice; its counts are zero.
        so_far = a["counts"] or [0, 0, 0]
        if not same:
            waiting = (b["posted"] == "nothing" and not b["record"]["read_after_month"]
                       and b["record"]["status"] == "available")
            reason = ("the record's last war-log read was before the month ended; the summary waits for a read after "
                      "the month. The record's counts so far equal the old count"
                      if waiting and b["record"]["counts"] == so_far and a["posted"] in ("chart", "notice") else "unexplained")
        rows.append({**{key: a[key] for key in ("clan", "month")}, "old": [a["posted"], a["counts"], a["completed"]],
                     "new": [b["posted"], b["counts"], b["completed"]], "record": b.get("record"), "same": same,
                     "reason": reason})
    return rows


def compare(work, old_path, new_path, out):
    old = json.loads(Path(old_path).read_text(encoding="utf-8"))
    new = json.loads(Path(new_path).read_text(encoding="utf-8"))
    prepared = json.loads((Path(work) / "prepared.json").read_text(encoding="utf-8"))
    record = Record(work)
    report = {"as_of": prepared["as_of"], "last_war_log_check": prepared["last_war_log_check"],
              "import": prepared["import"], "features": compare_features(old, new, new["by_identity"]),
              "monthly": compare_monthly(old, new),
              "agent": compare_agent(record, old, new, prepared["scenarios"])}
    report["unexplained"] = (
        [f"board {row['clan_code']}" for row in report["features"]["boards"] if "unexplained" in row["reasons"]]
        + [f"monthly {row['clan']} {row['month']}" for row in report["monthly"] if row["reason"] == "unexplained"]
        + [f"agent {row['name']}" for row in report["agent"]["scenarios"] if row["unexplained"]]
        + [f"current war {row['clan_code']}" for row in report["agent"]["current_war"] if row["reason"] == "unexplained"])
    Path(out).write_text(json.dumps(report, indent=1, sort_keys=True, default=str), encoding="utf-8")
    return {"unexplained": report["unexplained"], "written": str(out)}


if __name__ == "__main__":
    mode, *paths = sys.argv[1:]
    if mode == "prepare":
        outcome = asyncio.run(prepare(*paths))
    elif mode == "capture":
        outcome = asyncio.run(capture(*paths))
    elif mode == "compare":
        outcome = compare(*paths)
    else:
        raise SystemExit("Expected prepare, capture or compare")
    print(json.dumps(outcome, indent=1, default=str))
