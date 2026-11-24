"""Read SQLite datasets with table access and bounded execution."""

from __future__ import annotations

import asyncio
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from functools import partial
import json
import math
import re
import sqlite3
import time
import zlib

from elbow_helper.agent.safety.access import can_access_message_channel, has_reader_access_requirements, require_evidence_access
from elbow_helper.agent.reads.member_commands import MemberCommands
from elbow_helper.configuration.roles import AGENT_TESTER_ROLE_ID

QUERY_SECONDS = 20.0  # a full pass over the largest table takes ~10s; tool timeout is 30s
DEFAULT_QUERY_ROWS = 1000
MAX_QUERY_VALUE_BYTES = 8 * 1024 * 1024
MAX_QUERY_RESULT_BYTES = 16 * 1024 * 1024
QUALIFIED_NAMES = re.compile(r"\b(\w+)\s*\.\s*(\w+)\b")
STATE_NAMES = re.compile(r"\bstate\s*\.\s*(\w+)\b")


def _parameters(params):
    if not isinstance(params, dict) or any(
        not isinstance(key, str) or not re.fullmatch(r"\w+", key) for key in params
    ):
        raise ValueError("Use named parameters.")
    result = {}
    for key, value in params.items():
        if isinstance(value, (list, dict)):
            result[key] = json.dumps(value, allow_nan=False)
        elif value is None or type(value) in (str, int, float, bool):
            if type(value) is float and not math.isfinite(value):
                raise ValueError("Parameters must be finite numbers")
            result[key] = value
        else:
            raise ValueError("Parameter values must be scalars, lists or objects.")
    return result


def _cell(value):
    if isinstance(value, bytes):
        return value.hex()
    return value


def _unz(value):
    if value is None:
        return None
    try:
        decoder = zlib.decompressobj()
        data = decoder.decompress(value, MAX_QUERY_VALUE_BYTES)
        if decoder.unconsumed_tail:
            raise ValueError('The decompressed value is too large.')
        if not decoder.eof:
            raise ValueError('The compressed value is incomplete.')
        if decoder.unused_data:
            raise ValueError('The compressed value contains trailing data.')
        return data.decode('utf-8')
    except (TypeError, zlib.error, UnicodeError) as error:
        raise ValueError('The compressed value could not be read as UTF-8 text.') from error


def _army(value):
    if value is None:
        return None
    from elbow_helper.domain.armies import decode_army
    try:
        result = decode_army(value)
        if 'unparsed_code' not in result:
            return json.dumps(result, ensure_ascii=False)
    except (ValueError, OverflowError):
        pass
    raise ValueError(f'Army code could not be read: {value}')


_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _unix_us(value):
    if value is None:
        return None
    try:
        instant = datetime.fromisoformat(value)
        if instant.tzinfo is None:
            instant = instant.replace(tzinfo=timezone.utc)
        return (instant.astimezone(timezone.utc) - _EPOCH) // timedelta(microseconds=1)
    except (TypeError, OverflowError) as error:
        raise ValueError(str(error)) from error


def _iso_utc(value):
    if value is None:
        return None
    try:
        return (_EPOCH + timedelta(microseconds=value)).isoformat(timespec='microseconds')
    except (TypeError, OverflowError) as error:
        raise ValueError(str(error)) from error


def _identifiers(sql):
    identifiers = re.sub(r"'(?:''|[^'])*'|--[^\n]*|/\*.*?\*/", " ", sql, flags=re.S)
    return re.sub(
        "\"(\\w+)\"|`(\\w+)`|\\[(\\w+)\\]",
        lambda match: next(value for value in match.groups() if value is not None),
        identifiers,
    ).lower()


def run_query(sources, paths, levels, sql, params=None, max_rows=DEFAULT_QUERY_ROWS, *,
              channel_ids=None, read_channels=None, member_command_facts=None):
    """Use a fresh connection; return touched levels separately from data."""
    if not isinstance(sql, str) or not 1 <= len(sql) <= 4000:
        return {"error": "SQL must be between 1 and 4,000 characters."}, set()
    if max_rows is not None and (type(max_rows) is not int or not 1 <= max_rows <= 1000):
        return {"error": "Choose between 1 and 1,000 rows."}, set()
    # Strip literals, comments and quoted identifiers before checking placeholders.
    plain = re.sub(
        r"'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|--[^\n]*|/\*.*?\*/", "", sql, flags=re.S,
    )
    if re.search(r"\?|[@$]\w+", plain):
        return {"error": "Use named parameters in the form :name.",
                "cause": "invalid_arguments", "invalid_fields": ["sql", "params"],
                "instruction": "Replace each placeholder with :name and bind params by that name, "
                               "for example WHERE id = :id with params {\"id\": 7}."}, set()
    try:
        bindings = _parameters(params or {})
    except (ValueError, TypeError, OverflowError):
        return {"error": "Parameter values must be named scalars, lists or objects."}, set()
    identifiers = _identifiers(sql)
    qualified = dict.fromkeys(QUALIFIED_NAMES.findall(identifiers))
    aliases = {alias for alias, _ in qualified}
    # SQLite also resolves unqualified table names across attached databases.
    unqualified = set(re.findall(r"\b\w+\b", QUALIFIED_NAMES.sub(" ", identifiers)))
    declared = {
        (source.alias, name): level for source in sources for name, level, note in source.tables
    }
    states = {source.state_name: source for source in sources if source.state_name}
    state_names = states.keys() & set(STATE_NAMES.findall(identifiers))
    for alias, table in qualified:
        level = declared.get((alias, table))
        if alias.lower() == "state" and table in states:
            level = states[table].level
        if level and level != "none" and level not in levels:
            return {"error": f"This needs {level} access.", "required_access": [level], "cause": "access_denied"}, set()
    touched = set()
    read_tables = set()
    denied = set()
    hidden = {(source.alias, table, column) for source in sources for table, column in source.hidden_columns}
    hidden_read = set()
    visible_read = set()
    schemas = {}
    function_error = {}
    deadline = time.monotonic() + QUERY_SECONDS
    try:
        with closing(sqlite3.connect("file::memory:", uri=True)) as connection:
            # The progress handler cannot interrupt one oversized scalar
            # allocation. Enforce that bound inside SQLite before execution.
            connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, MAX_QUERY_VALUE_BYTES)
            connection.row_factory = sqlite3.Row

            def register(name, function):
                def call(value):
                    try:
                        return function(value)
                    except ValueError as error:
                        # SQLite otherwise hides the useful decoder failure.
                        function_error['error'] = str(error)
                        raise
                connection.create_function(name, 1, call, deterministic=True)

            register('unz', _unz)
            register('army', _army)
            register('unix_us', _unix_us)
            register('iso_utc', _iso_utc)
            attachments = []
            for source in sources:
                if source.state_name or (
                    source.alias not in aliases and not any(name in unqualified for name, _, _ in source.tables)
                ):
                    continue
                readable = {
                    name: level for name, level, note in source.tables
                    if level == "none" or level in levels
                }
                path = source.resolve(paths)
                if not readable or (source.snapshot is None and not path.is_file()):
                    continue
                attachments.append((source, path, readable))
            limit = connection.getlimit(sqlite3.SQLITE_LIMIT_ATTACHED)
            if len(attachments) + bool(state_names) > limit:
                return {"error": f"Use fewer databases in one query (maximum {limit}).", "cause": "capacity"}, set()

            allowed = {}
            for source, path, readable in attachments:
                if source.snapshot is not None:
                    selected = {name: level for name, level in readable.items()
                                if (source.alias, name) in qualified or name in unqualified}
                    tables = source.snapshot(paths, path, selected)
                    connection.execute(f'ATTACH DATABASE \':memory:\' AS "{source.alias}"')
                    for name, (columns, rows) in tables.items():
                        names = ','.join('"' + column + '" ' + kind for column, kind in columns)
                        connection.execute(f'CREATE TABLE "{source.alias}"."{name}" ({names})')
                        connection.executemany(
                            f'INSERT INTO "{source.alias}"."{name}" VALUES ({",".join("?" for _ in columns)})', rows)
                else:
                    connection.execute(
                        f"ATTACH DATABASE ? AS \"{source.alias}\"",
                        (path.resolve().as_uri() + "?mode=ro",),
                    )
                allowed.update({(source.alias, name): level for name, level in readable.items()})
                for name, level in readable.items():
                    if (source.alias, name) in qualified:
                        table_info = connection.execute(
                            f'PRAGMA "{source.alias}".table_info("{name}")').fetchall()
                        schemas[f"{source.alias}.{name}"] = {
                            "access": level,
                            "columns": {row[1]: row[2] for row in table_info
                                        if (source.alias, name, row[1]) not in hidden},
                            "primary_key": [row[1] for row in sorted(table_info, key=lambda row: row[5]) if row[5]],
                        }
            if state_names:
                connection.execute("ATTACH DATABASE ':memory:' AS state")
            for name in state_names:
                source = states[name]
                if source.level != "none" and source.level not in levels:
                    return {
                        "error": f"This needs {source.level} access.",
                        "required_access": [source.level],
                        "cause": "access_denied",
                    }, set()
                try:
                    doc = source.resolve(paths).read_text(encoding="utf-8")
                    data = json.loads(doc)
                    if source.excluded_keys:
                        if not isinstance(data, dict):
                            raise ValueError("Invalid object state")
                        data = {key: value for key, value in data.items() if key not in source.excluded_keys}
                        doc = json.dumps(data)
                    if source.evidence_channels is not None:
                        evidence_channels = source.evidence_channels(data)
                        if channel_ids is None or not evidence_channels <= channel_ids:
                            return {"error": "That conversation is not available here."}, set()
                        if read_channels is not None:
                            read_channels.update(evidence_channels)
                    if source.channel_keyed:
                        if channel_ids is None or not isinstance(data, dict):
                            raise ValueError("Missing channel scope or invalid channel state")
                        data = {key: value for key, value in data.items()
                                if key.isdecimal() and int(key) in channel_ids}
                        doc = json.dumps(data)
                        if read_channels is not None:
                            read_channels.update(int(key) for key in data)
                except (OSError, ValueError):
                    return {"error": f"{name} is unavailable right now."}, set()
                connection.execute(f"CREATE TABLE state.\"{name}\" (doc TEXT)")
                connection.execute(f"INSERT INTO state.\"{name}\" VALUES (?)", (doc,))
                allowed[("state", name)] = source.level
            # Initialize JSON virtual tables before prohibiting schema operations.
            connection.execute("SELECT value FROM json_each('[]')").fetchall()
            connection.execute("SELECT value FROM json_tree('[]')").fetchall()
            connection.execute("PRAGMA query_only = ON")
            if not connection.in_transaction:
                connection.execute("BEGIN")

            def authorize(action, arg1, arg2, database, trigger):
                if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_RECURSIVE):
                    return sqlite3.SQLITE_OK
                if action == sqlite3.SQLITE_FUNCTION:
                    return (
                        sqlite3.SQLITE_DENY if (arg2 or "").lower() == "load_extension"
                        else sqlite3.SQLITE_OK
                    )
                if action == sqlite3.SQLITE_READ:
                    if database is None:
                        return sqlite3.SQLITE_OK
                    if database in (None, "main") and arg1 in ("json_each", "json_tree"):
                        return sqlite3.SQLITE_OK
                    if (database, arg1, arg2) in hidden:
                        # Reads as NULL, so the stored value never reaches the model.
                        hidden_read.add(arg2.lower())
                        return sqlite3.SQLITE_IGNORE
                    level = allowed.get((database, arg1))
                    if level is not None:
                        read_tables.add((database, arg1))
                        if arg2:
                            visible_read.add(arg2.lower())
                        if level != "none":
                            touched.add(level)
                        return sqlite3.SQLITE_OK
                    level = declared.get((database, arg1))
                    if level and level != "none" and level not in levels:
                        denied.add(level)
                return sqlite3.SQLITE_DENY

            connection.set_authorizer(authorize)
            connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
            query = sql.strip().rstrip(";")
            cursor = connection.execute(query, bindings)
            columns = [column[0] for column in cursor.description or ()]
            if len(set(columns)) != len(columns):
                return {"error": "Use unique result column names."}, set()
            cap = max_rows if max_rows is not None else 100000
            raw_rows, result_bytes, total_rows = [], 0, 0
            size_limited = False
            for row in cursor:
                total_rows += 1
                if size_limited or len(raw_rows) == cap:
                    if max_rows is None:
                        return {"error": "The export is too large; narrow the query."}, set()
                    # Count the remainder of this cursor, without executing the
                    # user's query twice or retaining omitted rows.
                    continue
                # Bound the retained result while fetching, before an export
                # or a wide query can allocate its full result in Python.
                result_bytes += sum(2 * len(value) if isinstance(value, bytes) else
                                    len(value.encode('utf-8')) if isinstance(value, str) else 32
                                    for value in row)
                if result_bytes > MAX_QUERY_RESULT_BYTES or (len(raw_rows) + 1) * len(columns) > 100000:
                    if not raw_rows or max_rows is None:
                        return {"error": "The query result is too large; narrow it or aggregate.", "cause": "capacity"}, set()
                    size_limited = True
                    continue
                raw_rows.append(row)
            truncated = total_rows > len(raw_rows)
            cursor_columns = columns
            # Leave out a hidden column unless a visible read could have supplied it.
            blank = {index for index, name in enumerate(columns)
                     if name.lower() in hidden_read and name.lower() not in visible_read
                     and all(row[index] is None for row in raw_rows)}
            if len(blank) == len(columns):
                blank = set()
            columns = [name for index, name in enumerate(columns) if index not in blank]
            result = {
                "source": "bot_data",
                "rows": [
                    {
                        key: _cell(value)
                        for index, (key, value) in enumerate(zip(cursor_columns, row))
                        if index not in blank
                    }
                    for row in raw_rows
                ],
                "row_count": len(raw_rows),
                "truncated": truncated,
            }
            if truncated:
                result["total_rows"] = total_rows
                result["rows_cut"] = total_rows - len(raw_rows)
                result["instruction"] = (
                    "The result is incomplete: " + (
                        "it is over the size limit for one read, so select fewer columns, "
                        "filter or group the query."
                        if size_limited else
                        f"{max_rows} rows were allowed. "
                        + ("Raise max_rows (up to 1000) when the whole set fits, otherwise "
                           "filter or group the query." if max_rows < 1000 else
                           "Filter or group the query.")
                    ) + " Do not present these rows as the full answer."
                )
            if not raw_rows:
                result["columns"] = columns
            coverage = {
                source.alias: source.coverage(connection, bindings, min(cap, 200))
                for source in sources if source.coverage is not None and any(
                    (source.alias, table) in read_tables for table, _, _ in source.tables
                )
            }
            if coverage:
                result["source_coverage"] = coverage
            if member_command_facts is not None and ("discord", "member_roles") in read_tables:
                member_ids = {
                    value for row in result["rows"] for key, value in row.items()
                    if key.lower() in {"member_id", "discord_id", "discord_user_id"} and type(value) is int
                }
                facts = member_command_facts(member_ids)
                if facts:
                    result["member_commands"] = facts
            return result, touched
    except ValueError as error:
        return {"error": str(error)}, set()
    except sqlite3.Error as error:
        if function_error:
            return function_error, touched
        if denied:
            return {
                "error": "This needs " + ", ".join(sorted(denied)) + " access.",
                "required_access": sorted(denied),
                "cause": "access_denied",
            }, set()
        if time.monotonic() >= deadline:
            return {"error": "The query took too long. Narrow it or aggregate."}, set()
        if isinstance(error, sqlite3.DataError):
            return {"error": "The query value is too large; narrow it or aggregate.", "cause": "capacity"}, set()
        if str(error).startswith(("no such column:", "no such table:")):
            schema_access = {item["access"] for item in schemas.values() if item["access"] != "none"}
            return {"error": str(error), "cause": "sql_schema_error", "available_schema": schemas,
                    "instruction": "Use only the listed columns for these tables. Correct the query; do not repeat it or guess column names."}, schema_access
        return {"error": str(error)}, set()
    except OSError:
        return {"error": "The selected data is unavailable right now."}, set()


def reader_levels(context):
    """Dataset access levels this reader holds within the request scope."""
    from elbow_helper.agent.safety.access import KNOWN_ACCESS_REQUIREMENTS
    levels = {
        level for level in KNOWN_ACCESS_REQUIREMENTS
        if has_reader_access_requirements(context.guild, context.member.id, {level})
    }
    scope = context.state.scope
    if scope is not None:
        levels &= scope.levels
    return levels


async def query_context(context, sql, params=None, max_rows=DEFAULT_QUERY_ROWS, *, sources=None):
    from elbow_helper.agent.reads.sql.catalogue import SOURCES
    from elbow_helper.agent.reads.sql.reviewed import CLOSED, scoped_sources, current_roles_snapshot
    from elbow_helper.agent.safety.disclosure import exclude_source
    sources = SOURCES if sources is None else sources
    await require_evidence_access(context)
    levels = reader_levels(context)
    identifiers = _identifiers(sql) if isinstance(sql, str) else ""
    state_names = set(STATE_NAMES.findall(identifiers))
    channel_ids = set()
    if any((source.channel_keyed or source.evidence_channels is not None)
           and source.state_name in state_names
           and (source.level == "none" or source.level in levels) for source in sources) or any(
               source.alias in ("workflows", "activity") and any(
                   name.lower() in identifiers and (level == "none" or level in levels)
                   for name, level, _ in source.tables) for source in sources):
        for channel in (*context.guild.channels, *context.guild.threads):
            if await can_access_message_channel(context, channel, relevant=False):
                channel_ids.add(channel.id)
    read_channels = set()
    sources = scoped_sources(sources, channel_ids, read_channels)
    lookup_member_commands = None
    if ("member_roles" in identifiers or "member_commands" in identifiers) and any(
        source.snapshot is current_roles_snapshot for source in sources
    ):
        commands = MemberCommands.for_request(context)
        members = [(member.id, member.roles) for member in context.guild.members]
        roles_by_member = {
            member_id: frozenset(role.id for role in roles) for member_id, roles in members
        }
        rows = (
            (member_id, role.id, role.name) for member_id, roles in members
            for role in roles if not role.is_default() and role.id != AGENT_TESTER_ROLE_ID
        )
        command_rows = (
            (member_id, path, ", ".join(grant["through_roles"]))
            for member_id, held in roles_by_member.items()
            for grant in commands.named_grants(held)
            for path in grant["paths"]
        )
        sources = tuple(replace(source, snapshot=partial(
                            current_roles_snapshot, rows=rows, command_rows=command_rows))
                        if source.snapshot is current_roles_snapshot else source for source in sources)

        def member_command_facts(member_ids):
            return [{"member_id": member_id, **commands.facts(roles_by_member[member_id])}
                    for member_id in sorted(member_ids & roles_by_member.keys())]

        lookup_member_commands = member_command_facts

    result, touched = await asyncio.to_thread(
        run_query, sources, context.bot.paths, levels, sql, params, max_rows,
        channel_ids=channel_ids, read_channels=read_channels, member_command_facts=lookup_member_commands,
    )
    if "required_access" in result:
        if CLOSED in result["required_access"]:
            return {"error": "That data is unavailable through SQL.", "cause": "access_denied"}
        for level in result["required_access"]:
            exclude_source(context, "dataset:" + level, "role_scope", relevant=has_reader_access_requirements(
                context.guild, context.member.id, {level}))
        if context.state.scope_audit.private_candidates:
            result = {**result, "available_privately": True}
    # Decoder errors can name a value from a row, so their sources must survive
    # too and be checked again before any result leaves this handler.
    context.state.source_channels.update(read_channels)
    await require_evidence_access(context)
    if touched and not has_reader_access_requirements(context.guild, context.member.id, touched):
        return {
            "error": "This needs " + ", ".join(sorted(touched)) + " access.",
            "required_access": sorted(touched),
            "cause": "access_denied",
        }
    if "error" not in result or "available_schema" in result:
        context.state.required_access.update(touched)
    return result


async def query_bot_data(context, arguments):
    return await query_context(
        context, arguments["sql"], arguments.get("params"), arguments.get("max_rows", DEFAULT_QUERY_ROWS),
    )
