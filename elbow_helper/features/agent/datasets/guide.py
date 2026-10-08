"""Cache access-specific guides from feature schemas at process setup."""
from contextlib import closing
import sqlite3

from elbow_helper.configuration.clans import CLANS, CLAN_ORDER
from .catalogue import SOURCES

HEADER = """\
SQLite dialect. Name tables alias.table. State files are state.name with one JSON column doc; \
use json_each/json_extract. Pass lists as one named parameter: \
IN (SELECT value FROM json_each(:name)). Columns ending _ts are Unix seconds UTC \
(datetime(x, 'unixepoch')); _utc and _at text columns are ISO 8601 unless noted otherwise.
member_id, discord_user_id and user_id are Discord member IDs. player_tag is an uppercase \
Clash account tag (#...). clan_code is a family clan code. One member can link many accounts; \
links.links maps accounts to members. Join accounts across links, health and rosters by \
player_tag."""


class DataGuide:
    def __init__(self, paths, sources=SOURCES):
        self.entries = []
        self.variants = {}
        for source in sources:
            path = source.resolve(paths)
            if not path.is_file():
                continue
            if source.state_name:
                self.entries.append((
                    source.level,
                    f"state.{source.state_name} [{source.level}]: {source.note}; doc TEXT",
                ))
                continue
            try:
                with closing(sqlite3.connect(
                    path.resolve().as_uri() + "?mode=ro", uri=True,
                )) as connection:
                    for table, level, note in source.tables:
                        columns = connection.execute(f"PRAGMA table_info(\"{table}\")").fetchall()
                        if columns:
                            self.entries.append((
                                level,
                                f"{source.alias}.{table} [{level}]: {note}\n"
                                + ", ".join(_column(row) for row in columns),
                            ))
            except sqlite3.Error:
                continue
        self.clans = "\n".join(
            f"{code}: {CLANS[code].name}, {CLANS[code].tag}, utility={CLANS[code].is_utility}, "
            f"member={CLANS[code].member_role_id}, war={CLANS[code].war_role_id}, "
            f"CWL={CLANS[code].cwl_role_id}, leadership={CLANS[code].leadership_role_id}"
            for code in CLAN_ORDER
        )

    def for_levels(self, levels):
        key = frozenset(levels)
        if key not in self.variants:
            entries = [text for level, text in self.entries if level == "none" or level in key]
            unavailable = [
                text.split(":", 1)[0] for level, text in self.entries
                if level != "none" and level not in key
            ]
            self.variants[key] = "\n".join([HEADER, "Family clans:", self.clans, *entries,
                *([
                    "Not available to this asker: " + ", ".join(unavailable) + "."
                ] if unavailable else [])])
        return self.variants[key]


def _column(row):
    name, kind = row[1], row[2]
    unit = (
        " (Unix s UTC)" if name.endswith("_ts")
        else " (ISO UTC)" if kind.upper() == "TEXT" and name.endswith(("_utc", "_at")) else ""
    )
    return f"{name} {kind}{unit}"
