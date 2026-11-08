"""Account review state beside the family ownership record."""

import sqlite3

def create_schema(conn: sqlite3.Connection) -> None:
    cursor = conn.cursor()
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS suggestions (
            player_tag TEXT PRIMARY KEY,
            player_name TEXT NOT NULL,
            current_clan_code TEXT NOT NULL,
            current_clan_tag TEXT NOT NULL,
            proposed_discord_user_id INTEGER NOT NULL,
            proposed_display_name TEXT DEFAULT '',
            review_channel_id INTEGER DEFAULT 0,
            review_message_id INTEGER DEFAULT 0
        )
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS ignored_tags (
            player_tag TEXT PRIMARY KEY
        )
        """
    )


