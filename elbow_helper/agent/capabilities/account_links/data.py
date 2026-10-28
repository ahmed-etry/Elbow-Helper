"""Stored account links datasets available to agent queries."""

from elbow_helper.features.account_links.config import DB_PATH

from ...datasets.sources import DataSource


SOURCES = (
    DataSource(
        "links",
        DB_PATH,
        tables=(
            (
                "links", "none",
                "Links Clash account tags to Discord members. One member can have "
                "multiple linked accounts. Includes the last recorded account name, "
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
    ),
)
