"""Record commands use the same public feature operations as slash commands."""

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from elbow_helper.configuration.roles import LEAD_PLUS
from elbow_helper.features.agent.actions.contracts import ActionClass, check_bundle
from elbow_helper.features.agent.commands.adapters.records import (
    prepare_record_add, prepare_record_add_undo, prepare_record_edit,
    prepare_record_edit_undo, prepare_record_remove,
    record_adapters, run_record_add, run_record_edit, run_record_export,
    run_record_remove,
)
from elbow_helper.features.agent.commands.registry import build_command_capabilities
from elbow_helper.features.agent.models import AgentTurnState
from elbow_helper.features.help.catalog import HELP_ENTRIES
from elbow_helper.features.help.discovery import DiscoveredCommand, ParameterInfo
from elbow_helper.features.records.domain.types import INCIDENT_TYPES
from elbow_helper.features.records.service import RecordService


class RecordCommandPatternTests(unittest.IsolatedAsyncioTestCase):
    def test_all_record_help_commands_have_a_typed_adapter(self):
        paths = {entry.path for entry in HELP_ENTRIES if entry.path.startswith("/record ")}
        commands = {
            path: DiscoveredCommand(path, "registered", (
                ParameterInfo("record", "Choose a record.", True, "string"),
            ) if path.endswith(" remove") else ()) for path in paths
        }
        with patch("elbow_helper.features.agent.commands.registry.discover_commands",
                   return_value=commands):
            capabilities = build_command_capabilities(object(), record_adapters())
        self.assertEqual(set(capabilities), {
            "run_command_" + path.removeprefix("/").replace(" ", "_")
            for path in paths
        })
        self.assertEqual(capabilities["run_command_record_remove"].definition.parameters[
            "properties"]["record"]["type"], "integer")
        self.assertEqual(capabilities["run_command_record_edit"].required,
                         ("record",))
        self.assertIn("old and new values",
                      capabilities["run_command_record_edit"].definition.description)
        self.assertNotIn("Opens controls",
                         capabilities["run_command_record_edit"].definition.description)

    def setUp(self):
        self.member = SimpleNamespace(id=4, mention="@member", display_name="Member")
        self.asker = SimpleNamespace(
            id=5, roles=[SimpleNamespace(id=next(iter(LEAD_PLUS)))],
            display_name="Asker",
        )
        self.record = {
            "id": 7, "member_id": 4, "status": "active",
            "category_key": INCIDENT_TYPES[0].category_key,
            "incident_type_key": INCIDENT_TYPES[0].key,
            "note": "Synthetic details", "updated_ts": 100,
        }
        self.service = SimpleNamespace(
            validate_details=MagicMock(side_effect=RecordService.validate_details),
            create=MagicMock(return_value=dict(self.record)),
            active_record=MagicMock(return_value=dict(self.record)),
            edit=MagicMock(return_value=dict(self.record)),
            remove=MagicMock(return_value={**self.record, "status": "removed"}),
            edit_options=MagicMock(return_value=[dict(self.record)]),
            confirmation=MagicMock(return_value="Recorded synthetic incident."),
            edit_confirmation=MagicMock(return_value="Updated record #7 for Member."),
            display_name=MagicMock(return_value="Member"),
        )
        self.exports = SimpleNamespace(create=AsyncMock(), discard=AsyncMock())
        workflow = SimpleNamespace(service=self.service, exports=self.exports)
        guild = SimpleNamespace(
            get_member=lambda member_id: self.asker if member_id == self.asker.id else self.member,
            fetch_member=AsyncMock(return_value=self.member),
        )
        self.context = SimpleNamespace(
            bot=SimpleNamespace(get_cog=lambda _: workflow), guild=guild,
            member=self.asker, state=AgentTurnState(),
        )
        self.add_values = {
            "user": 4, "category": INCIDENT_TYPES[0].category_key,
            "type": INCIDENT_TYPES[0].key, "note": "Synthetic details",
        }

    async def test_add_previews_then_calls_the_public_create_operation(self):
        preview = await prepare_record_add(self.context, self.add_values)
        self.assertEqual(preview.lines[0],
                         "Add a record for @member: CWL, Missed Attack.")
        self.assertTrue(await preview.recheck())
        self.assertEqual(preview.before, {"record": None})
        self.service.create.assert_not_called()
        result = await run_record_add(self.context, self.add_values)
        self.assertEqual(result.visibility, "private")
        self.assertEqual(result.after["record_id"], 7)
        self.service.create.assert_called_once()

    async def test_add_undo_uses_the_public_remove_operation(self):
        undo = await prepare_record_add_undo(self.context, {
            "targets": self.add_values, "after": {"record_id": 7, "member_id": 4},
        })
        self.assertTrue(await undo.preview.recheck())
        result = await undo.run()
        self.assertEqual(result.status, "complete")
        self.service.remove.assert_called_once()

    async def test_preview_rechecks_current_record_access(self):
        preview = await prepare_record_add(self.context, self.add_values)
        self.asker.roles = []
        self.assertFalse(await preview.recheck())
        self.service.create.assert_not_called()

    async def test_unverified_add_is_reported_as_uncertain(self):
        self.service.active_record.return_value = None
        with self.assertRaises(OSError):
            await run_record_add(self.context, self.add_values)
        self.service.create.assert_called_once()

    async def test_remove_is_irreversible_and_stale_previews_stop(self):
        preview = await prepare_record_remove(self.context, {
            "user": 4, "record": 7,
        })
        self.assertTrue(await preview.recheck())
        self.record["note"] = "Changed"
        self.service.active_record.return_value = dict(self.record)
        self.assertFalse(await preview.recheck())
        adapters = {item.path: item for item in record_adapters()}
        self.assertIs(adapters["/record remove"].classification, ActionClass.IRREVERSIBLE)
        self.assertEqual(adapters["/record add"].classification, ActionClass.CHANGE)
        self.assertIs(adapters["/record export"].classification, ActionClass.OUTPUT)
        self.assertIs(adapters["/record edit"].classification, ActionClass.CHANGE)
        from elbow_helper.features.agent.actions.contracts import PreparedAction
        with self.assertRaises(ValueError):
            check_bundle((
                PreparedAction("add", {}, await prepare_record_add(self.context, self.add_values),
                               AsyncMock()),
                PreparedAction("remove", {}, preview, AsyncMock(),
                               ActionClass.IRREVERSIBLE),
            ))

    async def test_edit_previews_old_and_new_values_and_can_be_undone(self):
        values = {"user": 4, "record": 7, "note": "Revised details"}
        preview = await prepare_record_edit(self.context, values)
        self.assertIn("Details: Synthetic details to Revised details", preview.lines)
        self.assertTrue(await preview.recheck())
        edited = {**self.record, "note": "Revised details", "updated_ts": 101}
        self.service.edit.return_value = edited
        self.service.active_record.return_value = edited
        outcome = await run_record_edit(self.context, values)
        self.assertEqual(outcome.visibility, "private")
        self.assertEqual(outcome.after["record"]["note"], "Revised details")
        self.service.edit.assert_called_once()

        undo = await prepare_record_edit_undo(self.context, {
            "before": preview.before, "after": outcome.after,
        })
        self.assertTrue(await undo.preview.recheck())
        self.assertIn("Details: Revised details to Synthetic details", undo.preview.lines)
        self.service.edit.return_value = dict(self.record)
        self.service.active_record.return_value = dict(self.record)
        restored = await undo.run()
        self.assertEqual(restored.after["record"]["note"], "Synthetic details")
        self.assertEqual(self.service.edit.call_count, 2)

    async def test_edit_asks_for_a_change_before_preparing(self):
        outcome = await prepare_record_edit(self.context, {"user": 4, "record": 7})
        self.assertEqual(outcome.status, "needs_input")
        self.service.edit.assert_not_called()

    async def test_export_uses_the_public_export_service(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "report.xlsx"
            path.write_bytes(b"synthetic")
            self.exports.create.return_value = SimpleNamespace(
                google_link=None, google_warning=None,
                workbook_path=path, workbook_name="report.xlsx",
            )
            outcome = await run_record_export(self.context, {"user": 4})
        self.assertEqual(outcome.visibility, "private")
        self.assertEqual(outcome.attachments[0].data, b"synthetic")
        self.exports.create.assert_awaited_once()
        self.exports.discard.assert_awaited_once()

    async def test_remove_uses_the_public_feature_operation(self):
        self.service.active_record.return_value = None
        result = await run_record_remove(self.context, {"user": 4, "record": 7})
        self.assertEqual(result.status, "complete")
        self.service.remove.assert_called_once()
