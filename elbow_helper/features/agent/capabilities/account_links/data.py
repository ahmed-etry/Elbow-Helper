"""Stored account links datasets available to agent queries."""

from elbow_helper.features.account_links.config import DB_PATH

from ...datasets.sources import DataSource


SOURCES = (
    DataSource(
        "links",
        DB_PATH,
        tables=(
            (
                "links", "lead",
                "One linked account; discord_user_id owns player_tag; is_primary marks "
                "the primary account; last_seen_role is Clash member/admin/coLeader/leader.",
            ),
            ("ignored_tags", "lead", "One ignored account tag."),
            (
                "suggestions", "lead",
                "One account awaiting link review; proposed_discord_user_id and "
                "proposed_display_name are account links' stored owner verdict, "
                "not verified ownership.",
            ),
        ),
        config_constant="elbow_helper.features.account_links.config.DB_PATH",
    ),
)
