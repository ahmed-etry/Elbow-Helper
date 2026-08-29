from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock
from unittest.mock import MagicMock

from elbow_helper.features.clan_health.export import ClanHealthExportMixin
from elbow_helper.features.cwl.roster.export import CwlRosterExportMixin
from elbow_helper.features.records.commands import RecordCommandMixin
from elbow_helper.infrastructure.exports import ExportColumn
from elbow_helper.infrastructure.exports import ExportSheet
from elbow_helper.infrastructure.exports import LocalExportStore
from elbow_helper.infrastructure.exports import WorkbookWriter


def _attachment_message(url: str) -> MagicMock:
    message = MagicMock(attachments=[SimpleNamespace(url=url)])
    message.edit = AsyncMock()
    return message


class ExportDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_records_discard_local_file_after_discord_accepts_it(self) -> None:
        with TemporaryDirectory() as directory:
            workbook = Path(directory) / "records.xlsx"
            workbook.write_bytes(b"xlsx")
            report = SimpleNamespace(
                workbook_path=workbook,
                workbook_name="leadership_records.xlsx",
                google_link=None,
                google_warning="Google Sheets isn't available here.",
            )
            interaction = MagicMock()
            interaction.followup.send = AsyncMock(
                return_value=_attachment_message(
                    "https://discord.test/leadership_records.xlsx"
                )
            )
            commands = object.__new__(RecordCommandMixin)
            commands.exports = MagicMock()
            commands.exports.discard = AsyncMock()

            await commands._send_record_export(interaction, report)

        commands.exports.discard.assert_awaited_once_with(report)

    async def test_clan_health_removes_staging_file_after_discord_fallback(self) -> None:
        with TemporaryDirectory() as directory:
            export = object.__new__(ClanHealthExportMixin)
            export.local_exports = LocalExportStore(Path(directory))
            export.google_publisher = MagicMock()
            export.google_publisher.upload_workbook = AsyncMock(
                return_value=(None, "Google Sheets isn't available here.")
            )
            interaction = MagicMock()
            interaction.followup.send = AsyncMock(
                return_value=_attachment_message(
                    "https://discord.test/clan_health.xlsx"
                )
            )

            await export._write_and_send_export(
                interaction=interaction,
                workbook_name="clan_health.xlsx",
                workbook_title="Clan Health",
                summary_lines=["Clan Health"],
                sheets=[("Overview", [["Clan", "Members"], ["BEH", 50]])],
            )

            self.assertEqual(list(Path(directory).glob("*.xlsx")), [])

    async def test_cwl_roster_removes_staging_file_after_discord_fallback(self) -> None:
        with TemporaryDirectory() as directory:
            export = object.__new__(CwlRosterExportMixin)
            export.cwl_exports = LocalExportStore(Path(directory))
            export.workbook_writer = WorkbookWriter()
            export.google_publisher = MagicMock()
            export.google_publisher.create_spreadsheet = AsyncMock(
                return_value=(None, "Google Sheets isn't available here.")
            )
            interaction = MagicMock()
            interaction.guild.name = "Brown Elbow"
            interaction.followup.send = AsyncMock(
                return_value=_attachment_message(
                    "https://discord.test/cwl_roster.xlsx"
                )
            )
            sheet = ExportSheet(
                title="Roster Planner",
                columns=(ExportColumn("Account", 150),),
                rows=(("Ahmad",),),
                tab_color="3B5B92",
            )

            await export._send_roster_workbook(
                interaction=interaction,
                sheets=[sheet],
                history_label="July 2026",
                signed_member_count=1,
                signed_account_count=1,
            )

            self.assertEqual(list(Path(directory).glob("*.xlsx")), [])


if __name__ == "__main__":
    unittest.main()
