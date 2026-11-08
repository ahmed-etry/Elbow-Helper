"""Stored account links datasets available to agent queries."""

from elbow_helper.features.account_links.config import DB_PATH
from elbow_helper.configuration.files import FAMILY_DATABASE
from elbow_helper.data.links.selectors import query_links
from contextlib import closing
import sqlite3

from elbow_helper.agent.reads.sql.sources import DataSource


def link_snapshot(paths, database_path, readable):
    result = {}
    if 'links' in readable:
        result['links'] = query_links(paths.data_root / FAMILY_DATABASE)
    # Review tables share the family file. Ownership is projected above.
    auxiliary = set(readable) & {'suggestions', 'ignored_tags'}
    if auxiliary and database_path.is_file():
        with closing(sqlite3.connect(database_path.resolve().as_uri() + '?mode=ro', uri=True)) as connection:
            for table in sorted(auxiliary):
                columns = [(row[1], row[2] + (' NOT NULL' if row[3] else '')
                            + (' PRIMARY KEY' if row[5] else ''))
                           for row in connection.execute(f'PRAGMA table_info({table})')]
                result[table] = (columns, connection.execute(f'SELECT * FROM {table}').fetchall())
    return result


SOURCES = (
    DataSource(
        "links",
        DB_PATH,
        tables=(
            (
                "links", "none",
                "One linked Clash account per row: player_tag to its Discord member "
                "discord_user_id. Join health tables on player_tag = links.links.player_tag (LEFT JOIN: "
                "unlinked accounts have no row) and GROUP BY discord_user_id for one row per member. "
                "One member can have several accounts. Includes the last recorded account name, "
                "clan and clan role. These links are separate from ClashPerk's.",
            ),
            ("ignored_tags", "recruiter_or_core", "One ignored account tag."),
            (
                "suggestions", "recruiter_or_core",
                "One account awaiting link review; proposed_discord_user_id and "
                "proposed_display_name are account links' stored owner verdict, "
                "not verified ownership.",
            ),
        ),
        config_constant="elbow_helper.features.account_links.config.DB_PATH",
        snapshot=link_snapshot,
    ),
)
