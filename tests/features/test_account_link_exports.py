"""Record exports show every linked account without account-status labels."""

from pathlib import Path
import tempfile
import unittest
from xml.etree import ElementTree
import zipfile

from elbow_helper.features.records.sheets.export import RecordWorkbookWriter
from elbow_helper.infrastructure.exports import WorkbookWriter


class AccountLinkExportTests(unittest.TestCase):
    def test_all_record_sheets_keep_accounts_without_primary_label(self):
        links = {7: [
            {"player_tag": "#P0", "player_name_last_seen": "Alpha", "is_primary": 1},
            {"player_tag": "#P2", "player_name_last_seen": "Beta", "is_primary": 0},
            {"player_tag": "#P8", "player_name_last_seen": ""},
        ]}
        records = [{"member_id": 7, "member_display": "Member", "category_key": "war",
                    "incident_type_key": "missed_attack", "created_ts": 1}]
        expected = "Alpha (#P0)\nBeta (#P2)\n#P8 (#P8)"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "records.xlsx"
            RecordWorkbookWriter(WorkbookWriter()).write(
                path, records, links, include_empty_categories=False,
            )
            with zipfile.ZipFile(path) as workbook:
                sheets = [name for name in workbook.namelist()
                          if name.startswith("xl/worksheets/sheet") and name.endswith(".xml")]
                self.assertEqual(len(sheets), 3)
                for name in sheets:
                    root = ElementTree.fromstring(workbook.read(name))
                    texts = [node.text for node in root.iter()
                             if node.tag.endswith("}t")]
                    self.assertIn(expected, texts)
                    self.assertFalse(any("[Primary]" in text for text in texts if text))
