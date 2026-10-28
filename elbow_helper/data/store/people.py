"""Atomic observations of people, names, roles and shadow ownership."""

from elbow_helper.infrastructure.persistence.sqlite import sqlite_transaction

from . import FamilyStore
from .projections import discord_projection, link_projection, search_key


def member(connection, discord_id, at):
    connection.execute(
        "INSERT INTO members(discord_id,first_observed_at,last_observed_at,presence_status) "
        "VALUES (?,?,?,'unknown') ON CONFLICT(discord_id) DO NOTHING", (discord_id, at, at),
    )
    return connection.execute("SELECT id FROM members WHERE discord_id=?", (discord_id,)).fetchone()[0]


def account(connection, tag, at):
    connection.execute(
        "INSERT INTO accounts(player_tag,first_observed_at,tracking_status) VALUES (?,?,'observed') "
        "ON CONFLICT(player_tag) DO NOTHING", (tag, at),
    )
    return connection.execute("SELECT id FROM accounts WHERE player_tag=?", (tag,)).fetchone()[0]


def close_interval(connection, table, row, at, item, *, basis=None):
    if at < row["last_observed_at"]:
        raise ValueError("out_of_order_observation")
    fields = "to_at=?,end_previous_good_at=last_observed_at,end_source_item_id=?"
    values = [at, item]
    if basis is not None:
        fields += ",end_basis=?"
        values.append(basis)
    connection.execute(f"UPDATE {table} SET {fields} WHERE id=?", (*values, row["id"]))


def alias(connection, table, identity_column, identity, name, at, item, *, kind=None):
    where = f"{identity_column}=?" + (" AND kind=?" if kind is not None else "")
    params = (identity, kind) if kind is not None else (identity,)
    current = connection.execute(f"SELECT * FROM {table} WHERE {where} AND to_at IS NULL", params).fetchone()
    if current and current["name"] == name:
        return
    if current:
        if at < current["from_at"]:
            raise ValueError("out_of_order_observation")
        connection.execute(
            f"UPDATE {table} SET to_at=?,end_source_item_id=? WHERE {where} AND to_at IS NULL",
            (at, item, *params),
        )
    if name is not None:
        columns = f"{identity_column}," + ("kind," if kind is not None else "")
        values = (*params, name, search_key(name), at, item)
        connection.execute(
            f"INSERT INTO {table}({columns}name,search_key,from_at,source_item_id) "
            f"VALUES ({','.join('?' for _ in values)})", values,
        )


def account_name(connection, account_id, name, at, item):
    alias(connection, "account_aliases", "account_id", account_id, name, at, item)
    connection.execute("UPDATE accounts SET current_name=? WHERE id=?", (name, account_id))


def mirror_links(connection, run, payload, at):
    item = FamilyStore.item(connection, run, 0)["id"]
    rows = link_projection(payload)  # A malformed snapshot cannot establish unlinking.
    with sqlite_transaction(connection, immediate=True):
        before = connection.total_changes
        previous = FamilyStore.previous_good(connection, "account_links", "current")
        current = {row["account_id"]: row for row in connection.execute(
            "SELECT * FROM account_links WHERE to_at IS NULL",
        )}
        seen = set()
        for row in rows:
            account_id = account(connection, row["player_tag"], at)
            member_id = member(connection, row["discord_user_id"], at)
            seen.add(account_id)
            old = current.get(account_id)
            if old and at < old["last_observed_at"]:
                raise ValueError("out_of_order_observation")
            if old and old["member_id"] != member_id:
                close_interval(connection, "account_links", old, at, item)
            if old is None or old["member_id"] != member_id:
                connection.execute(
                    "INSERT INTO account_links(account_id,member_id,from_at,previous_good_at,"
                    "last_observed_at,basis,source_item_id) VALUES (?,?,?,?,?,'shadow_observed',?)",
                    (account_id, member_id, at, previous, at, item),
                )
            else:
                connection.execute("UPDATE account_links SET last_observed_at=? WHERE id=?", (at, old["id"]))
            connection.execute(
                "INSERT INTO account_link_metadata(account_id,player_name_last_seen,last_seen_clan_tag,"
                "last_seen_clan_code,last_seen_role,observed_at,basis,source_item_id) "
                "VALUES (?,?,?,?,?,NULL,'legacy_unknown_age',?) ON CONFLICT(account_id) DO UPDATE SET "
                "player_name_last_seen=excluded.player_name_last_seen,last_seen_clan_tag=excluded.last_seen_clan_tag,"
                "last_seen_clan_code=excluded.last_seen_clan_code,last_seen_role=excluded.last_seen_role,"
                "source_item_id=excluded.source_item_id",
                (account_id, row["player_name_last_seen"], row["last_seen_clan_tag"], row["last_seen_clan_code"],
                 row["last_seen_role"], item),
            )
        for account_id, old in current.items():
            if account_id not in seen:
                close_interval(connection, "account_links", old, at, item)
        FamilyStore.outcome(connection, item, "ok" if rows else "empty", at, count=len(rows),
                            written=connection.total_changes - before)


def observe_member(connection, row, at, item, previous, basis):
    member_id = member(connection, row["id"], at)
    old = connection.execute("SELECT * FROM members WHERE id=?", (member_id,)).fetchone()
    if at < old["last_observed_at"]:
        raise ValueError("out_of_order_observation")
    known_at = old["last_observed_at"] if old["presence_status"] != "unknown" else None
    bounds = [value for value in (known_at, previous) if value is not None]
    last_confirmed = max(bounds) if bounds else None
    connection.execute(
        "UPDATE members SET current_username=?,display_name=?,nick=?,last_observed_at=?,bot=?,"
        "presence_status='present' WHERE id=?",
        (row["username"], row["display_name"], row["nick"], at, row["bot"], member_id),
    )
    for kind, value in (("username", row["username"]), ("display_name", row["display_name"]), ("nick", row["nick"])):
        alias(connection, "member_aliases", "member_id", member_id, value, at, item, kind=kind)
    current = connection.execute(
        "SELECT * FROM guild_membership WHERE member_id=? AND to_at IS NULL", (member_id,),
    ).fetchone()
    if current is None:
        connection.execute(
            "INSERT INTO guild_membership(member_id,from_at,discord_joined_at,previous_good_at,"
            "last_observed_at,start_basis,source_item_id) VALUES (?,?,?,?,?,?,?)",
            (member_id, at, row["joined_at"], last_confirmed, at, basis, item),
        )
    else:
        connection.execute("UPDATE guild_membership SET last_observed_at=? WHERE id=?", (at, current["id"]))
    roles = {role["role_id"]: role for role in connection.execute(
        "SELECT * FROM member_roles WHERE member_id=? AND to_at IS NULL", (member_id,),
    )}
    for role_id, old in roles.items():
        if role_id not in row["roles"]:
            close_interval(connection, "member_roles", old, at, item)
    for role_id in row["roles"]:
        if role_id not in roles:
            connection.execute(
                "INSERT INTO member_roles(member_id,role_id,from_at,previous_good_at,last_observed_at,"
                "observation_basis,source_item_id) VALUES (?,?,?,?,?,?,?)",
                (member_id, role_id, at, last_confirmed, at, basis, item),
            )
        else:
            connection.execute("UPDATE member_roles SET last_observed_at=? WHERE id=?", (at, roles[role_id]["id"]))


def leave_member(connection, member_id, at, item, basis):
    old_member = connection.execute("SELECT last_observed_at FROM members WHERE id=?", (member_id,)).fetchone()
    if at < old_member[0]:
        raise ValueError("out_of_order_observation")
    connection.execute("UPDATE members SET presence_status='departed',last_observed_at=? WHERE id=?", (at, member_id))
    for table in ("guild_membership", "member_roles"):
        for old in connection.execute(f"SELECT * FROM {table} WHERE member_id=? AND to_at IS NULL", (member_id,)):
            close_interval(connection, table, old, at, item, basis=basis if table == "guild_membership" else None)


def observe_discord(connection, run, rows, at, *, complete=False, basis="scan", departed_id=None, scope="guild"):
    rows = discord_projection(rows)
    item = FamilyStore.item(connection, run, 0)["id"]
    with sqlite_transaction(connection, immediate=True):
        before = connection.total_changes
        previous = FamilyStore.previous_good(connection, "discord_scan", scope)
        for row in rows:
            observe_member(connection, row, at, item, previous, basis)
        if departed_id is not None:
            leave_member(connection, member(connection, departed_id, at), at, item, "gateway_leave")
        elif complete:
            seen = {row["id"] for row in rows}
            for old in connection.execute("SELECT id,discord_id FROM members WHERE presence_status='present'"):
                if old["discord_id"] not in seen:
                    leave_member(connection, old["id"], at, item, "complete_scan_absence")
        partial = basis == "scan" and not complete
        FamilyStore.outcome(connection, item, "failed" if partial else "ok" if rows or departed_id else "empty", at,
                            error="partial_member_scan" if partial else None, count=len(rows),
                            complete=not partial, written=connection.total_changes - before)
