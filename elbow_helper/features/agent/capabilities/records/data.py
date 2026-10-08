"""Stored records datasets available to agent queries."""

from elbow_helper.features.records.config import DB_PATH

from ...datasets.sources import DataSource


SOURCES = (
    DataSource(
        "records",
        DB_PATH,
        tables=(
            (
                "leadership_records", "lead_plus",
                "One leadership record; status active/removed; removed records remain "
                "historical; timestamp columns are UTC Unix seconds.",
            ),
        ),
        config_constant="elbow_helper.features.records.config.DB_PATH",
    ),
)
