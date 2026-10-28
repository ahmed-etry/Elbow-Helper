"""Read SQLite datasets with table access and bounded execution."""

from __future__ import annotations

import asyncio
from contextlib import closing
import json
import math
import re
import sqlite3
import time

from ..access import can_access_message_channel, has_reader_access_requirements, require_evidence_access

QUERY_SECONDS = 5.0
MAX_QUERY_VALUE_BYTES = 8 * 1024 * 1024
MAX_QUERY_RESULT_BYTES = 16 * 1024 * 1024
STATE_NAMES = re.compile(r"\bstate\.(\w+)\b", re.I)


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


def _cell(value, truncate=True):
    if isinstance(value, bytes):
        return value.hex()
    if truncate and isinstance(value, str) and len(value) > 4000:
        return value[:4000] + "…"
    return value


def _identifiers(sql):
    identifiers = re.sub(r"'(?:''|[^'])*'|--[^\n]*|/\*.*?\*/", "", sql, flags=re.S)
    return re.sub(
        "\"(\\w+)\"|`(\\w+)`|\\[(\\w+)\\]",
        lambda match: next(value for value in match.groups() if value is not None),
        identifiers,
    ).lower()


def run_query(sources, paths, levels, sql, params=None, max_rows=200, *,
              channel_ids=None, read_channels=None):
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
    declared = {
        (source.alias, name): level for source in sources for name, level, note in source.tables
    }
    states = {source.state_name: source for source in sources if source.state_name}
    for alias, table in re.findall(r"\b(\w+)\.(\w+)\b", identifiers):
        level = declared.get((alias, table))
        if alias.lower() == "state" and table in states:
            level = states[table].level
        if level and level != "none" and level not in levels:
            return {"error": f"This needs {level} access.", "required_access": [level]}, set()
    touched = set()
    read_tables = set()
    denied = set()
    schemas = {}
    deadline = time.monotonic() + QUERY_SECONDS
    try:
        with closing(sqlite3.connect("file::memory:", uri=True)) as connection:
            # The progress handler cannot interrupt one oversized scalar
            # allocation. Enforce that bound inside SQLite before execution.
            connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, MAX_QUERY_VALUE_BYTES)
            connection.row_factory = sqlite3.Row
            allowed = {}
            for source in sources:
                path = source.resolve(paths)
                readable = {
                    name: level for name, level, note in source.tables
                    if level == "none" or level in levels
                }
                if source.state_name or not readable or not path.is_file():
                    continue
                connection.execute(
                    f"ATTACH DATABASE ? AS \"{source.alias}\"",
                    (path.resolve().as_uri() + "?mode=ro",),
                )
                allowed.update({(source.alias, name): level for name, level in readable.items()})
                for name, level in readable.items():
                    if re.search(r"\b" + re.escape(source.alias + "." + name) + r"\b", identifiers):
                        table_info = connection.execute(
                            f'PRAGMA "{source.alias}".table_info("{name}")').fetchall()
                        schemas[f"{source.alias}.{name}"] = {
                            "access": level,
                            "columns": {row[1]: row[2] for row in table_info},
                            "primary_key": [row[1] for row in sorted(table_info, key=lambda row: row[5]) if row[5]],
                        }
            connection.execute("ATTACH DATABASE ':memory:' AS state")
            for name in set(STATE_NAMES.findall(identifiers)):
                source = states.get(name)
                if source is None:
                    continue
                if source.level != "none" and source.level not in levels:
                    return {
                        "error": f"This needs {source.level} access.",
                        "required_access": [source.level],
                    }, set()
                try:
                    doc = source.resolve(paths).read_text(encoding="utf-8")
                    data = json.loads(doc)
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
                    level = allowed.get((database, arg1))
                    if level is not None:
                        read_tables.add((database, arg1))
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
            for row in cursor:
                total_rows += 1
                if len(raw_rows) == cap:
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
                    return {"error": "The query result is too large; narrow it or aggregate.", "cause": "capacity"}, set()
                raw_rows.append(row)
            truncated = total_rows > cap
            result = {
                "rows": [
                    {
                        key: _cell(value, truncate=max_rows is not None)
                        for key, value in zip(columns, row)
                    }
                    for row in raw_rows[:cap]
                ],
                "row_count": min(len(raw_rows), cap),
                "truncated": truncated,
            }
            if truncated:
                result["total_rows"] = total_rows
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
            return result, touched
    except ValueError as error:
        return {"error": str(error)}, set()
    except sqlite3.Error as error:
        if denied:
            return {
                "error": "This needs " + ", ".join(sorted(denied)) + " access.",
                "required_access": sorted(denied),
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


async def query_context(context, sql, params=None, max_rows=200):
    from .catalogue import SOURCES
    from ..access import KNOWN_ACCESS_REQUIREMENTS
    from ..disclosure import exclude_source
    await require_evidence_access(context)
    levels = {
        level for level in KNOWN_ACCESS_REQUIREMENTS
        if has_reader_access_requirements(context.guild, context.member.id, {level})
    }
    scope = context.state.scope
    if scope is not None:
        levels &= scope.levels
    state_names = set(STATE_NAMES.findall(_identifiers(sql))) if isinstance(sql, str) else set()
    channel_ids = set()
    if any((source.channel_keyed or source.evidence_channels is not None)
           and source.state_name in state_names
           and (source.level == "none" or source.level in levels) for source in SOURCES):
        for channel in (*context.guild.channels, *context.guild.threads):
            if await can_access_message_channel(context, channel, relevant=False):
                channel_ids.add(channel.id)
    read_channels = set()
    result, touched = await asyncio.to_thread(
        run_query, SOURCES, context.bot.paths, levels, sql, params, max_rows,
        channel_ids=channel_ids, read_channels=read_channels,
    )
    if "required_access" in result:
        for level in result["required_access"]:
            exclude_source(context, "dataset:" + level, "role_scope", relevant=has_reader_access_requirements(
                context.guild, context.member.id, {level}))
        if context.state.scope_audit.private_candidates:
            result = {**result, "available_privately": True}
    if "error" not in result:
        context.state.source_channels.update(read_channels)
    await require_evidence_access(context)
    if touched and not has_reader_access_requirements(context.guild, context.member.id, touched):
        return {
            "error": "This needs " + ", ".join(sorted(touched)) + " access.",
            "required_access": sorted(touched),
        }
    if "error" not in result or "available_schema" in result:
        context.state.required_access.update(touched)
    return result


async def query_bot_data(context, arguments):
    return await query_context(
        context, arguments["sql"], arguments.get("params"), arguments.get("max_rows", 200),
    )
