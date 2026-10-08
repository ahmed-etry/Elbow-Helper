from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from elbow_helper.features.agent.conversation.state import Conversation, ConversationRecord, ConversationTurn
from features.agent.report_helpers import make_event_report
from elbow_helper.features.agent.capabilities.events.report import EventScheduleReport
from elbow_helper.features.agent.capabilities.member_lifecycle.report import MemberLifecycleReport
from elbow_helper.features.agent.capabilities.clan_reporting.report import MissingElderReport
from elbow_helper.features.agent.knowledge.report import KnowledgeReport
from elbow_helper.features.agent.knowledge.store import KnowledgeStore
from elbow_helper.features.agent.capabilities.cwl.report import (
    CwlAssScopeReport, CwlPerformanceReport,
)
from elbow_helper.features.agent.capabilities.cwl.bonus_report import CwlBonusScopeReport
from elbow_helper.features.agent.capabilities.clan_transfers.report import TransferQueueReport
from elbow_helper.features.agent.capabilities.hibernation.report import HibernationReport
from elbow_helper.features.agent.capabilities.support_tickets.report import SupportTicketReport
from elbow_helper.features.agent.capabilities.recruitment.report import RecruitmentTrialReport
from elbow_helper.features.agent.capabilities.examination.report import ExaminationCaseReport
from elbow_helper.configuration.channels import HIBERNATION_LOG
from elbow_helper.features.clan_transfers.config import CLAN_TRANSFER_QUEUES
from elbow_helper.features.clan_transfers.queries import (
    PendingTransferRequest, TransferQueueSnapshot,
)
from elbow_helper.features.hibernation.queries import (
    ActiveHibernationRecord, ActiveHibernationSnapshot,
)
from elbow_helper.features.support_tickets.queries import (
    SupportTicketMetadata, SupportTicketSnapshot,
)
from elbow_helper.features.recruitment.queries import (
    ActiveRecruitmentTrial, ActiveRecruitmentTrialSnapshot,
)
from elbow_helper.features.examination.queries import (
    ExaminationCaseSnapshot, ExaminationCaseStatus,
)
from elbow_helper.features.agent.files.contracts import (
    CsvImportArtifact,
    TextImportArtifact,
    XlsxImportArtifact,
    XlsxSheet,
)
from elbow_helper.features.cwl.queries import (
    CwlAssScopeRow, CwlAssScopeSnapshot, CwlBonusAttackScore,
    CwlBonusScopeSnapshot, CwlBonusSettings, CwlClanSeasonSummary,
    CwlPerformanceRow, CwlPerformanceSnapshot,
)
from elbow_helper.features.event_stats.queries import (
    EventScheduleRow, EventScheduleSnapshot,
)
from elbow_helper.features.member_lifecycle.queries import (
    MemberLifecycleRow, MemberLifecycleSnapshot, OverdueApplicant,
)
from elbow_helper.features.clan_reporting.queries import (
    MissingElderRow, MissingElderSnapshot,
)
from elbow_helper.configuration.channels import CLAN_LEADERSHIP_CHANNELS
from elbow_helper.features.agent.conversation.repository import ConversationRepository, SnapshotCapacityError, SnapshotConflict, StoredConversation
from elbow_helper.features.agent.conversation.codec import (
    encode_conversation as _encode_conversation, decode_conversation,
)
from elbow_helper.features.agent.conversation.context import build_history_checkpoint
from elbow_helper.features.agent.access import ACCESS_LEAD, ACCESS_LEAD_PLUS
from elbow_helper.features.agent.conversation.instructions import WorkingState
from elbow_helper.infrastructure.persistence import UnsupportedSQLiteVersionError


def _snapshot(root=1, *, guild=1, channel=100, touched=1000):
    return StoredConversation(guild, channel, root, 0, touched, touched + 21600, "{}")


def encode_conversation(conversation, **kwargs):
    """Give legacy codec fixtures explicit source provenance."""
    conversation.report_sources = {
        report_id: frozenset({conversation.channel_id})
        for report_id in conversation.reports
    }
    conversation.report_access_requirements = {
        report_id: frozenset() for report_id in conversation.reports
    }
    return _encode_conversation(conversation, **kwargs)


class AgentStorageTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "agent.sqlite3"
        self.repository = ConversationRepository(self.path)

    def test_reply_lookup_is_scoped_and_survives_reopening(self):
        self.repository.save(_snapshot(), (10,))
        reopened = ConversationRepository(self.path)
        self.assertEqual(reopened.find_reply(1, 100, 10, now=1001).root_message_id, 1)
        self.assertIsNone(reopened.find_reply(2, 100, 10, now=1001))
        self.assertIsNone(reopened.find_reply(1, 200, 10, now=1001))

    def test_stale_writer_cannot_replace_newer_snapshot(self):
        first = _snapshot()
        revision = self.repository.save(first, (10,))
        self.repository.save(replace(first, revision=revision, payload='{"new":true}'), (11,))
        with self.assertRaises(SnapshotConflict):
            self.repository.save(replace(first, revision=revision), (12,))
        self.assertIsNone(self.repository.find_reply(1, 100, 10, now=1001))
        self.assertEqual(self.repository.find_reply(1, 100, 11, now=1001).revision, 2)

    def test_reply_collision_rolls_back_entire_new_snapshot(self):
        self.repository.save(_snapshot(), (10,))
        with self.assertRaises(sqlite3.IntegrityError):
            self.repository.save(_snapshot(2), (10,))
        self.assertEqual([row.root_message_id for row in self.repository.load_active(now=1001, limit=128)], [1])
        self.assertEqual(self.repository.find_reply(1, 100, 10, now=1001).root_message_id, 1)

    def test_expiry_prevents_reads_and_prune_cascades_reply_ids(self):
        self.repository.save(_snapshot(), (10,))
        self.assertIsNone(self.repository.find_reply(1, 100, 10, now=22600))
        self.assertEqual(self.repository.load_active(now=22600, limit=128), ())
        self.assertEqual(self.repository.prune(now=22600), 1)
        with self.repository.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM conversation_replies").fetchone()[0], 0)

    def test_capacity_prunes_oldest_snapshot_and_its_reply_mapping(self):
        with patch("elbow_helper.features.agent.conversation.repository.MAX_CONVERSATIONS", 2):
            for index in range(1, 4):
                self.repository.save(_snapshot(index, touched=1000 + index), (10 + index,))
        self.assertIsNone(self.repository.find_reply(1, 100, 11, now=1004))
        self.assertEqual(len(self.repository.load_active(now=1004, limit=128)), 2)

    def test_capacity_never_evicts_protected_conversations(self):
        self.repository.save(_snapshot(1), (11,))
        self.repository.save(_snapshot(2, touched=1001), (12,))
        with patch("elbow_helper.features.agent.conversation.repository.MAX_CONVERSATIONS", 2):
            with self.assertRaises(SnapshotCapacityError):
                self.repository.save(_snapshot(3, touched=1002), (13,), protected_roots=(1, 2))
            self.repository.save(_snapshot(3, touched=1002), (13,), protected_roots=(1,))
        self.assertIsNotNone(self.repository.find_reply(1, 100, 11, now=1003))
        self.assertIsNone(self.repository.find_reply(1, 100, 12, now=1003))
        self.assertIsNotNone(self.repository.find_reply(1, 100, 13, now=1003))

    def test_expiry_pruning_defers_in_flight_conversations(self):
        self.repository.save(_snapshot(1), (11,))
        self.repository.save(_snapshot(2), (12,))
        self.assertEqual(self.repository.prune(now=22600, protected_roots=(1,)), 1)
        with self.repository.connect() as connection:
            rows = connection.execute("SELECT root_message_id FROM conversations").fetchall()
        self.assertEqual([row[0] for row in rows], [1])
        self.assertEqual(self.repository.prune(now=22600), 1)

    def test_oversize_write_leaves_existing_snapshot_intact(self):
        self.repository.save(_snapshot(), (10,))
        with patch("elbow_helper.features.agent.conversation.repository.MAX_SNAPSHOT_BYTES", 1):
            with self.assertRaises(ValueError):
                self.repository.save(replace(_snapshot(), revision=1), (11,))
        self.assertIsNotNone(self.repository.find_reply(1, 100, 10, now=1001))

    def test_sqlite_backup_can_be_reopened_with_reply_identity(self):
        self.repository.save(_snapshot(), (10,))
        backup_path = Path(self.directory.name) / "backup.sqlite3"
        with self.repository.connect() as source:
            destination = sqlite3.connect(backup_path)
            try:
                source.backup(destination)
            finally:
                destination.close()
        restored = ConversationRepository(backup_path)
        self.assertIsNotNone(restored.find_reply(1, 100, 10, now=1001))

    def test_newer_schema_is_rejected_without_modification(self):
        with self.repository.connect() as connection:
            connection.execute("PRAGMA user_version=2")
        with self.assertRaises(UnsupportedSQLiteVersionError):
            ConversationRepository(self.path)


class ConversationCodecTests(unittest.TestCase):

    def test_report_provenance_is_required_and_tampering_fails_closed(self):
        conversation = self.conversation()
        with self.assertRaises(ValueError):
            _encode_conversation(
                conversation, root_message_id=1, revision=0,
                wall_time=1000, monotonic_time=60,
            )

        snapshot = encode_conversation(
            conversation, root_message_id=1, revision=0,
            wall_time=1000, monotonic_time=60,
        )
        for field, value in (
            ("report_sources", {}),
            ("report_sources", {"report": []}),
            ("report_access_requirements", {"report": ["unknown"]}),
        ):
            data = json.loads(snapshot.payload)
            data[field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                decode_conversation(
                    replace(snapshot, payload=json.dumps(data)), wall_time=1001,
                )

    def test_legacy_snapshot_with_reports_is_rejected_but_empty_snapshot_restores(self):
        snapshot = encode_conversation(
            self.conversation(), root_message_id=1, revision=0,
            wall_time=1000, monotonic_time=60,
        )
        data = json.loads(snapshot.payload)
        data["format"] = 1
        data.pop("report_sources")
        data.pop("report_access_requirements")
        for turn in data["turns"]:
            turn.pop("required_access")
        with self.assertRaises(ValueError):
            decode_conversation(
                replace(snapshot, payload=json.dumps(data)), wall_time=1001,
            )

        data["reports"] = []
        restored = decode_conversation(
            replace(snapshot, payload=json.dumps(data)), wall_time=1001,
        )
        self.assertEqual(restored.reports, {})

    def test_turn_role_requirements_roundtrip_and_unknown_values_are_rejected(self):
        conversation = self.conversation()
        conversation.turns[0] = replace(
            conversation.turns[0],
            required_access=frozenset({ACCESS_LEAD_PLUS}),
        )
        snapshot = encode_conversation(
            conversation, root_message_id=1, revision=0,
            wall_time=1000, monotonic_time=60,
        )
        restored = decode_conversation(snapshot, wall_time=1001)
        self.assertEqual(
            restored.turns[0].required_access,
            frozenset({ACCESS_LEAD_PLUS}),
        )
        data = json.loads(snapshot.payload)
        data["turns"][0]["required_access"] = ["unknown"]
        with self.assertRaises(ValueError):
            decode_conversation(
                replace(snapshot, payload=json.dumps(data)), wall_time=1001,
            )

    def test_invalid_timestamps_and_identity_types_are_rejected(self):
        snapshot = encode_conversation(self.conversation(), root_message_id=1, revision=0, wall_time=1000, monotonic_time=60)
        for changes in ({"expires_at": float("nan")}, {"touched_at": float("inf")},
                        {"expires_at": snapshot.expires_at + 1}, {"guild_id": True}, {"revision": -1}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                decode_conversation(replace(snapshot, **changes), wall_time=1001)

    def test_duplicate_json_fields_and_coerced_reply_ids_are_rejected(self):
        snapshot = encode_conversation(self.conversation(), root_message_id=1, revision=0, wall_time=1000, monotonic_time=60)
        with self.assertRaises(ValueError):
            decode_conversation(replace(snapshot, payload='{"format":1,"format":1}'), wall_time=1001)
        for value in (True, "10", 10.5, -1):
            data = json.loads(snapshot.payload)
            data["reply_ids"] = [value]
            with self.subTest(value=value), self.assertRaises(ValueError):
                decode_conversation(replace(snapshot, payload=json.dumps(data)), wall_time=1001)

    def test_shorter_saved_expiry_is_not_extended_on_restore(self):
        snapshot = encode_conversation(self.conversation(), root_message_id=1, revision=0, wall_time=1000, monotonic_time=60)
        snapshot = replace(snapshot, expires_at=1200)
        restored = decode_conversation(snapshot, wall_time=1100, monotonic_time=10)
        self.assertEqual(restored.touched_at + 21600 - 10, 100)

    def test_duplicate_instruction_ids_are_rejected(self):
        snapshot = encode_conversation(self.conversation(), root_message_id=1, revision=0, wall_time=1000, monotonic_time=60)
        data = json.loads(snapshot.payload)
        data["working"]["instructions"] *= 2
        with self.assertRaises(ValueError):
            decode_conversation(replace(snapshot, payload=json.dumps(data)), wall_time=1001)

    def conversation(self):
        conversation = Conversation(1, 100)
        record = ConversationRecord(1, 42, "2026-09-17", "Question", "Generated answer",
                                    "Delivered part", "Context", ("Evidence",), ("report",), (10,), False)
        conversation.append(ConversationTurn('{"answer":"Delivered part"}', frozenset({100, 200}), record))
        conversation.reply_ids = [10]
        conversation.reports["report"] = make_event_report("report", "2026-09-17")
        conversation.working, _ = WorkingState().remember(label="Grouping", quote="Keep together", request_text="Keep together",
            member_id=42, message_id=1, channel_id=100, created_at="2026-09-17")
        conversation.touched_at = 50
        return conversation

    def test_roundtrip_preserves_records_reports_working_state_without_extending_expiry(self):
        original = self.conversation()
        snapshot = encode_conversation(original, root_message_id=1, revision=0, wall_time=1000, monotonic_time=60)
        restored = decode_conversation(snapshot, wall_time=1100, monotonic_time=10)
        self.assertEqual(restored.turns, original.turns)
        self.assertEqual(restored.reports, original.reports)
        self.assertEqual(restored.report_sources, original.report_sources)
        self.assertEqual(
            restored.report_access_requirements,
            original.report_access_requirements,
        )
        self.assertEqual(restored.working, original.working)
        self.assertEqual(restored.reply_ids, [10])
        self.assertEqual(restored.touched_at, -100)
        self.assertEqual(snapshot.expires_at, 22590)
        self.assertNotIn("reasoning_content", snapshot.payload)

    def test_delivery_unknown_roundtrip_and_legacy_defaults_fail_closed(self):
        original = self.conversation()
        record = replace(
            original.turns[0].record,
            delivery_unknown=True,
            attempted_nonces=(123, 456),
            uncertain_nonce=456,
        )
        original.turns[0] = replace(original.turns[0], record=record)
        snapshot = encode_conversation(
            original, root_message_id=1, revision=0,
            wall_time=1000, monotonic_time=60,
        )
        restored = decode_conversation(snapshot, wall_time=1001)
        self.assertEqual(restored.turns[0].record, record)

        for field, value in (
            ("uncertain_nonce", 999),
            ("attempted_nonces", [123, 123]),
            ("delivery_complete", True),
        ):
            data = json.loads(snapshot.payload)
            data["turns"][0]["record"][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                decode_conversation(
                    replace(snapshot, payload=json.dumps(data)), wall_time=1001,
                )

        data = json.loads(snapshot.payload)
        legacy_record = data["turns"][0]["record"]
        for field in (
            "delivery_unknown", "attempted_nonces", "uncertain_nonce",
        ):
            legacy_record.pop(field)
        legacy_record["delivery_complete"] = False
        legacy = decode_conversation(
            replace(snapshot, payload=json.dumps(data)), wall_time=1001,
        )
        self.assertFalse(legacy.turns[0].record.delivery_unknown)
        self.assertEqual(legacy.turns[0].record.attempted_nonces, ())

    def test_checkpoint_roundtrip_recomputes_digest_and_rejects_tampering(self):
        original = self.conversation()
        seed = original.turns[0]
        original.turns = [replace(
            seed,
            record=replace(
                seed.record, request_message_id=index,
                reply_ids=(1000 + index,),
            ),
        ) for index in range(1, 9)]
        original.checkpoint = build_history_checkpoint(
            original.turns, covered_turn_count=8,
            created_at=datetime(2026, 9, 17, 12, tzinfo=timezone.utc),
        )
        snapshot = encode_conversation(
            original, root_message_id=1, revision=0,
            wall_time=1000, monotonic_time=60,
        )
        restored = decode_conversation(snapshot, wall_time=1001, monotonic_time=61)
        self.assertEqual(restored.checkpoint, original.checkpoint)

        for path, value in (
            (("summary",), "forged"),
            (("input_hash",), "0" * 64),
            (("covered_request_ids",), [999]),
            (("source_channels",), [100]),
        ):
            data = json.loads(snapshot.payload)
            target = data["checkpoint"]
            target[path[0]] = value
            with self.subTest(path=path), self.assertRaises(ValueError):
                decode_conversation(
                    replace(snapshot, payload=json.dumps(data)), wall_time=1001,
                )

    def test_expired_and_future_format_payloads_are_rejected(self):
        snapshot = encode_conversation(self.conversation(), root_message_id=1, revision=0, wall_time=1000, monotonic_time=60)
        with self.assertRaises(ValueError):
            decode_conversation(snapshot, wall_time=snapshot.expires_at)
        data = json.loads(snapshot.payload)
        data["format"] = 3
        with self.assertRaises(ValueError):
            decode_conversation(replace(snapshot, payload=json.dumps(data)), wall_time=1001)

    def test_unknown_report_kind_does_not_silently_restore_partial_state(self):
        snapshot = encode_conversation(self.conversation(), root_message_id=1, revision=0, wall_time=1000, monotonic_time=60)
        data = json.loads(snapshot.payload)
        data["reports"][0]["kind"] = "future-kind"
        with self.assertRaises(ValueError):
            decode_conversation(replace(snapshot, payload=json.dumps(data)), wall_time=1001)

    def test_cwl_performance_report_roundtrip_and_guild_scope(self):
        original = self.conversation()
        performance = CwlPerformanceSnapshot(
            "2026-09-17T12:00:00+00:00", 3, ("2026-08",),
            (CwlClanSeasonSummary("2026-08", "BEH", "Champion League II", 1007,
                                  7, 1, 7, 7, True),),
            (CwlPerformanceRow(
                "2026-08", "BEH", "Champion League II", "high_2026_06",
                "#P0", "Alpha", 18, 7, 7, 7, 21, 100.0, 21.0, 1, 1,
                21.0, 1, 1,
            ),),
        )
        original.reports.clear()
        original.reports["cwl"] = CwlPerformanceReport("cwl", 1, performance)
        snapshot = encode_conversation(
            original, root_message_id=1, revision=0, wall_time=1000, monotonic_time=60,
        )

        restored = decode_conversation(snapshot, wall_time=1001, monotonic_time=61)
        self.assertEqual(restored.reports["cwl"], original.reports["cwl"])

        data = json.loads(snapshot.payload)
        data["reports"][0]["guild_id"] = 2
        with self.assertRaises(ValueError):
            decode_conversation(replace(snapshot, payload=json.dumps(data)), wall_time=1001)

    def test_scoped_cwl_ass_roundtrip_preserves_selected_scope_projection(self):
        original = self.conversation()
        original.reports.clear()
        report = CwlAssScopeReport(
            "ass", 1, CwlAssScopeSnapshot(
                "2026-09-17T12:00:00+00:00", "BE1", "2026-09",
                "war", None, "CWL:#WAR", ("CWL:#WAR",), (3,), 1,
                "Master League II", "lower_2026_06",
                "Lower League Standard (Jun 2026)", 1.0,
                "lower_linear", "calculated_from_selected_scope",
                "selected_completed_wars", (CwlAssScopeRow(
                    "#P0", "Alpha", 18, 1, 1, 1, 3, 100.0,
                    2.0, 1.0, 1.0, 21.0, 1, 1,
                    21.0, 0.0, 0.0, 0.0,
                ),),
            ),
        )
        original.reports[report.report_id] = report
        snapshot = encode_conversation(
            original, root_message_id=1, revision=0,
            wall_time=1000, monotonic_time=60,
        )

        restored = decode_conversation(snapshot, wall_time=1001)

        self.assertEqual(restored.reports[report.report_id], report)
        data = json.loads(snapshot.payload)
        data["reports"][0]["snapshot"]["rows"][0]["projected_stars"] = None
        with self.assertRaises(ValueError):
            decode_conversation(
                replace(snapshot, payload=json.dumps(data)), wall_time=1001,
            )

    def test_event_schedule_roundtrip_rejects_forged_counter_coverage(self):
        original = self.conversation()
        original.reports.clear()
        report = EventScheduleReport(
            "events", 1, EventScheduleSnapshot(
                "2026-09-19T12:00:00+00:00",
                (EventScheduleRow(
                    "members", "Members", "preset", "counter", "counter",
                    True, "live", 0, None, 0, None, None, None,
                    20, 2, 1, "partial_missing_roles",
                ),),
            ),
        )
        original.reports[report.report_id] = report
        original.report_sources[report.report_id] = frozenset({
            original.channel_id,
        })
        original.report_access_requirements[report.report_id] = frozenset({
            ACCESS_LEAD,
        })
        snapshot = _encode_conversation(
            original, root_message_id=1, revision=0,
            wall_time=1000, monotonic_time=60,
        )

        restored = decode_conversation(snapshot, wall_time=1001)

        self.assertEqual(restored.reports[report.report_id], report)
        self.assertEqual(
            restored.report_access_requirements[report.report_id],
            frozenset({ACCESS_LEAD}),
        )
        data = json.loads(snapshot.payload)
        data["reports"][0]["snapshot"]["rows"][0]["count_coverage"] = (
            "complete"
        )
        with self.assertRaises(ValueError):
            decode_conversation(
                replace(snapshot, payload=json.dumps(data)), wall_time=1001,
            )

    def test_member_lifecycle_roundtrip_requires_embedded_source_provenance(self):
        original = self.conversation()
        original.reports.clear()
        report = MemberLifecycleReport(
            "lifecycle", 1, 900,
            MemberLifecycleSnapshot(
                "2026-09-19T12:00:00+00:00", 1, 1, 0, 0, 0, 0,
                None, "2026-09-19T06:00:00+00:00",
                (("Reddit", 1),),
                (OverdueApplicant(42, "Alpha"),),
                (MemberLifecycleRow(
                    42, "Alpha", "2026-09-18T10:00:00+00:00",
                    "Reddit", True, "2026-09-19T09:00:00+00:00", 901,
                ),),
            ),
        )
        original.reports[report.report_id] = report
        original.report_sources[report.report_id] = frozenset({900, 901})
        original.report_access_requirements[report.report_id] = frozenset()
        snapshot = _encode_conversation(
            original, root_message_id=1, revision=0,
            wall_time=1000, monotonic_time=60,
        )

        restored = decode_conversation(snapshot, wall_time=1001)
        self.assertEqual(restored.reports[report.report_id], report)

        original.report_sources[report.report_id] = frozenset({900})
        with self.assertRaises(ValueError):
            _encode_conversation(
                original, root_message_id=1, revision=0,
                wall_time=1000, monotonic_time=60,
            )

        data = json.loads(snapshot.payload)
        data["reports"][0]["snapshot"]["rows"][0][
            "last_seen_channel_id"
        ] = 902
        with self.assertRaises(ValueError):
            decode_conversation(
                replace(snapshot, payload=json.dumps(data)), wall_time=1001,
            )

    def test_missing_elder_roundtrip_requires_leadership_source_provenance(self):
        original = self.conversation()
        original.reports.clear()
        clan_code = next(iter(CLAN_LEADERSHIP_CHANNELS))
        channel_id = CLAN_LEADERSHIP_CHANNELS[clan_code]
        report = MissingElderReport(
            "missing-elder", 1, (channel_id,), (),
            MissingElderSnapshot(
                "2026-09-19T12:00:00+00:00", (clan_code,), 0,
                (MissingElderRow(
                    42, "Alpha", "#P2LQ", "One", clan_code, "member",
                ),),
            ),
        )
        original.reports[report.report_id] = report
        original.report_sources[report.report_id] = frozenset({channel_id})
        original.report_access_requirements[report.report_id] = frozenset()
        snapshot = _encode_conversation(
            original, root_message_id=1, revision=0,
            wall_time=1000, monotonic_time=60,
        )

        restored = decode_conversation(snapshot, wall_time=1001)
        self.assertEqual(restored.reports[report.report_id], report)

        original.report_sources[report.report_id] = frozenset({100})
        with self.assertRaises(ValueError):
            _encode_conversation(
                original, root_message_id=1, revision=0,
                wall_time=1000, monotonic_time=60,
            )

        data = json.loads(snapshot.payload)
        data["reports"][0]["source_channel_ids"] = [channel_id + 1]
        with self.assertRaises(ValueError):
            decode_conversation(
                replace(snapshot, payload=json.dumps(data)), wall_time=1001,
            )

    def test_approved_knowledge_roundtrip_requires_role_and_content_integrity(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        knowledge_path = Path(directory.name) / "knowledge"
        knowledge_path.mkdir()
        (knowledge_path / "policy.md").write_text(
            "+++\n"
            'key = "lead_policy"\n'
            "version = 1\n"
            'title = "Lead policy"\n'
            'topics = ["lead"]\n'
            'source_refs = ["owner-review:2026-09"]\n'
            'effective_from = "2026-01-01T00:00:00+00:00"\n'
            'reviewed_at = "2026-09-19T10:00:00+00:00"\n'
            "approved_by = 42\n"
            'visibility = "lead"\n'
            'status = "approved"\n'
            "+++\n"
            "Only current Lead members may read this.\n",
            encoding="utf-8",
        )
        section = KnowledgeStore(knowledge_path).load(
            now=datetime(2026, 9, 19, 12, tzinfo=timezone.utc),
        ).active_sections[0]
        report = KnowledgeReport(
            "knowledge", 1, "2026-09-19T12:00:00+00:00",
            "lead", ("lead",), (section,),
        )
        original = self.conversation()
        original.reports.clear()
        original.reports[report.report_id] = report
        original.report_sources[report.report_id] = frozenset({100})
        original.report_access_requirements[report.report_id] = frozenset({
            ACCESS_LEAD,
        })
        snapshot = _encode_conversation(
            original, root_message_id=1, revision=0,
            wall_time=1000, monotonic_time=60,
        )
        restored = decode_conversation(snapshot, wall_time=1001)
        self.assertEqual(restored.reports[report.report_id], report)

        original.turns[0] = replace(
            original.turns[0],
            knowledge_refs=((section.section_id, section.content_sha256),),
        )
        turn_snapshot = _encode_conversation(
            original, root_message_id=1, revision=0,
            wall_time=1000, monotonic_time=60,
        )
        restored = decode_conversation(turn_snapshot, wall_time=1001)
        self.assertEqual(
            restored.turns[0].knowledge_refs,
            ((section.section_id, section.content_sha256),),
        )
        malformed = json.loads(turn_snapshot.payload)
        malformed["turns"][0]["knowledge_refs"] = [[section.section_id]]
        with self.assertRaises(ValueError):
            decode_conversation(
                replace(turn_snapshot, payload=json.dumps(malformed)),
                wall_time=1001,
            )

        original.report_access_requirements[report.report_id] = frozenset()
        with self.assertRaises(ValueError):
            _encode_conversation(
                original, root_message_id=1, revision=0,
                wall_time=1000, monotonic_time=60,
            )

        data = json.loads(snapshot.payload)
        data["reports"][0]["sections"][0]["body"] = "tampered"
        with self.assertRaises(ValueError):
            decode_conversation(
                replace(snapshot, payload=json.dumps(data)), wall_time=1001,
            )

    def test_cwl_bonus_scope_roundtrip_rejects_inconsistent_coverage(self):
        original = self.conversation()
        original.reports.clear()
        report = CwlBonusScopeReport(
            "bonus", 1, CwlBonusScopeSnapshot(
                "2026-09-17T12:00:00+00:00", "BE1", "2026-09",
                "war", None, "#WAR", (3,), ("#WAR",),
                CwlBonusSettings(
                    4, "2026-09-01T00:00:00+00:00", 2, 8,
                    0.15, 0.10, 0, 0.20, 2.0,
                ),
                (), (), (CwlBonusAttackScore(
                    3, "#WAR", "#P0", "Alpha", 18, "#D0", 18,
                    3, 100.0, 3.0, 2.0, 0, "18:18", 1.0, 0.0,
                    1.0, 3, "",
                ),), (), "selected_scored_attacks",
            ),
        )
        original.reports[report.report_id] = report
        snapshot = encode_conversation(
            original, root_message_id=1, revision=0,
            wall_time=1000, monotonic_time=60,
        )

        restored = decode_conversation(snapshot, wall_time=1001)

        self.assertEqual(restored.reports[report.report_id], report)
        data = json.loads(snapshot.payload)
        data["reports"][0]["snapshot"]["coverage_status"] = (
            "no_matching_scored_attacks"
        )
        with self.assertRaises(ValueError):
            decode_conversation(
                replace(snapshot, payload=json.dumps(data)), wall_time=1001,
            )

    def test_pending_transfer_report_roundtrip_and_validation(self):
        original = self.conversation()
        original.reports.clear()
        created = datetime(2026, 9, 17, 5, tzinfo=timezone.utc)
        expires = datetime(2026, 9, 17, 17, tzinfo=timezone.utc)
        request = PendingTransferRequest(
            42, created.isoformat(), int(created.timestamp()),
            expires.isoformat(), int(expires.timestamp()),
        )
        queue = TransferQueueSnapshot(
            "BEH", CLAN_TRANSFER_QUEUES["BEH"]["thread_id"], 1, 0, (request,),
        )
        report = TransferQueueReport(
            "transfers", 1, "2026-09-17T12:00:00+00:00", 12, 1, 0, (queue,),
        )
        original.reports[report.report_id] = report
        snapshot = encode_conversation(
            original, root_message_id=1, revision=0,
            wall_time=1000, monotonic_time=60,
        )

        restored = decode_conversation(snapshot, wall_time=1001, monotonic_time=61)
        self.assertEqual(restored.reports[report.report_id], report)

        data = json.loads(snapshot.payload)
        data["reports"][0]["queues"][0]["pending"][0]["expires_ts"] += 1
        with self.assertRaises(ValueError):
            decode_conversation(
                replace(snapshot, payload=json.dumps(data)), wall_time=1001,
            )

        data = json.loads(snapshot.payload)
        data["reports"][0]["guild_id"] = 2
        with self.assertRaises(ValueError):
            decode_conversation(
                replace(snapshot, payload=json.dumps(data)), wall_time=1001,
            )

    def test_hibernation_report_roundtrip_excludes_private_state_and_validates(self):
        original = self.conversation()
        original.reports.clear()
        started = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)
        report = HibernationReport(
            "hibernation", 1, HIBERNATION_LOG,
            ActiveHibernationSnapshot(
                "2026-09-17T12:00:00+00:00", 1, 0, 2, 0,
                (ActiveHibernationRecord(
                    42, started.isoformat(), int(started.timestamp()), "recorded",
                ),),
            ),
        )
        original.reports[report.report_id] = report
        snapshot = encode_conversation(
            original, root_message_id=1, revision=0,
            wall_time=1000, monotonic_time=60,
        )

        restored = decode_conversation(snapshot, wall_time=1001, monotonic_time=61)
        self.assertEqual(restored.reports[report.report_id], report)
        self.assertNotIn("roles", snapshot.payload)
        self.assertNotIn("private_thread", snapshot.payload)

        data = json.loads(snapshot.payload)
        data["reports"][0]["snapshot"]["records"][0]["start_time_status"] = "private"
        with self.assertRaises(ValueError):
            decode_conversation(
                replace(snapshot, payload=json.dumps(data)), wall_time=1001,
            )

        data = json.loads(snapshot.payload)
        data["reports"][0]["source_channel_id"] = 1
        with self.assertRaises(ValueError):
            decode_conversation(
                replace(snapshot, payload=json.dumps(data)), wall_time=1001,
            )

        data = json.loads(snapshot.payload)
        data["reports"][0]["guild_id"] = 2
        with self.assertRaises(ValueError):
            decode_conversation(
                replace(snapshot, payload=json.dumps(data)), wall_time=1001,
            )

    def test_support_ticket_report_roundtrip_excludes_content_and_validates(self):
        original = self.conversation()
        original.reports.clear()
        created = datetime(2026, 9, 1, tzinfo=timezone.utc)
        activity = datetime(2026, 9, 16, 12, tzinfo=timezone.utc)
        report = SupportTicketReport(
            "support", 1, 2, 1,
            SupportTicketSnapshot(
                "2026-09-17T12:00:00+00:00",
                (SupportTicketMetadata(
                    200, "support-member", 42, "resolved", False,
                    created.isoformat(), int(created.timestamp()),
                    activity.isoformat(), int(activity.timestamp()), 86_400,
                    "channel_last_message",
                ),),
            ),
        )
        original.reports[report.report_id] = report
        snapshot = encode_conversation(
            original, root_message_id=1, revision=0,
            wall_time=1000, monotonic_time=60,
        )

        restored = decode_conversation(snapshot, wall_time=1001, monotonic_time=61)
        self.assertEqual(restored.reports[report.report_id], report)
        self.assertNotIn("message_content", snapshot.payload)
        self.assertNotIn("topic_text", snapshot.payload)

        data = json.loads(snapshot.payload)
        data["reports"][0]["snapshot"]["tickets"][0]["owner_status"] = "forged"
        with self.assertRaises(ValueError):
            decode_conversation(
                replace(snapshot, payload=json.dumps(data)), wall_time=1001,
            )

        data = json.loads(snapshot.payload)
        data["reports"][0]["guild_id"] = 2
        with self.assertRaises(ValueError):
            decode_conversation(
                replace(snapshot, payload=json.dumps(data)), wall_time=1001,
            )

    def test_recruitment_trial_report_roundtrip_excludes_private_state_and_validates(self):
        original = self.conversation()
        original.reports.clear()
        start = datetime(2026, 9, 16, 12, tzinfo=timezone.utc)
        expected_end = datetime(2026, 9, 18, 12, tzinfo=timezone.utc)
        report = RecruitmentTrialReport(
            "recruitment", 1,
            ActiveRecruitmentTrialSnapshot(
                "2026-09-17T12:00:00+00:00", 1, 0,
                (ActiveRecruitmentTrial(
                    200, 42,
                    start.isoformat(), int(start.timestamp()), 2,
                    expected_end.isoformat(), int(expected_end.timestamp()),
                    "in_progress", 86_400, None,
                ),),
            ),
        )
        original.reports[report.report_id] = report
        snapshot = encode_conversation(
            original, root_message_id=1, revision=0,
            wall_time=1000, monotonic_time=60,
        )

        restored = decode_conversation(snapshot, wall_time=1001, monotonic_time=61)
        self.assertEqual(restored.reports[report.report_id], report)
        self.assertNotIn("tracking_msg_id", snapshot.payload)
        self.assertNotIn("private_note", snapshot.payload)
        self.assertNotIn("application_answers", snapshot.payload)

        data = json.loads(snapshot.payload)
        data["reports"][0]["snapshot"]["trials"][0]["expected_end_ts"] += 1
        with self.assertRaises(ValueError):
            decode_conversation(
                replace(snapshot, payload=json.dumps(data)), wall_time=1001,
            )

        data = json.loads(snapshot.payload)
        data["reports"][0]["snapshot"]["trials"][0]["timing_status"] = "accepted"
        with self.assertRaises(ValueError):
            decode_conversation(
                replace(snapshot, payload=json.dumps(data)), wall_time=1001,
            )

        data = json.loads(snapshot.payload)
        data["reports"][0]["guild_id"] = 2
        with self.assertRaises(ValueError):
            decode_conversation(
                replace(snapshot, payload=json.dumps(data)), wall_time=1001,
            )

    def test_examination_report_roundtrip_excludes_private_state_and_validates(self):
        original = self.conversation()
        original.reports.clear()
        report = ExaminationCaseReport(
            "examination", 1,
            ExaminationCaseSnapshot(
                "2026-09-17T12:00:00+00:00", 1, 0,
                (ExaminationCaseStatus(
                    200, 42, "identified", "elder_promo", "not_applicable",
                    "required", "routed", "reminder", "not_recorded",
                    "followup_sent",
                ),),
            ),
        )
        original.reports[report.report_id] = report
        snapshot = encode_conversation(
            original, root_message_id=1, revision=0,
            wall_time=1000, monotonic_time=60,
        )

        restored = decode_conversation(snapshot, wall_time=1001, monotonic_time=61)
        self.assertEqual(restored.reports[report.report_id], report)
        for excluded in (
            "availability", "elder_reason", "pinged_ids", "routing_message_id",
            "application_answers",
        ):
            self.assertNotIn(excluded, snapshot.payload)

        data = json.loads(snapshot.payload)
        data["reports"][0]["snapshot"]["cases"][0]["workflow_status"] = "approved"
        with self.assertRaises(ValueError):
            decode_conversation(
                replace(snapshot, payload=json.dumps(data)), wall_time=1001,
            )

        data = json.loads(snapshot.payload)
        data["reports"][0]["snapshot"]["cases"][0]["response_status"] = "recorded"
        with self.assertRaises(ValueError):
            decode_conversation(
                replace(snapshot, payload=json.dumps(data)), wall_time=1001,
            )

        data = json.loads(snapshot.payload)
        data["reports"][0]["guild_id"] = 2
        with self.assertRaises(ValueError):
            decode_conversation(
                replace(snapshot, payload=json.dumps(data)), wall_time=1001,
            )

    def test_csv_import_roundtrip_and_source_scope(self):
        original = self.conversation()
        original.reports.clear()
        original.reports["csv"] = CsvImportArtifact(
            "csv", 1, 100, 50, 60, "season.csv", "text/csv", 12,
            "a" * 64, "2026-09-17T12:00:00+00:00", 1, "utf-8-sig", ",",
            ("Name", "Tag"), (("Alpha", "#P0"),), ("formula_like_cells:0",),
        )
        snapshot = encode_conversation(
            original, root_message_id=1, revision=0, wall_time=1000, monotonic_time=60,
        )
        restored = decode_conversation(snapshot, wall_time=1001, monotonic_time=61)
        self.assertEqual(restored.reports["csv"], original.reports["csv"])

        data = json.loads(snapshot.payload)
        data["reports"][0]["rows"][0].append("extra")
        with self.assertRaises(ValueError):
            decode_conversation(replace(snapshot, payload=json.dumps(data)), wall_time=1001)

    def test_xlsx_import_roundtrip_and_source_scope(self):
        original = self.conversation()
        original.reports.clear()
        original.reports["xlsx"] = XlsxImportArtifact(
            "xlsx", 1, 100, 50, 60, "season.xlsx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            12, 100, "a" * 64, "2026-09-17T12:00:00+00:00", 1,
            (XlsxSheet("Roster", ("Name", "Tag"), (("Alpha", "#P0"),), ()),),
        )
        snapshot = encode_conversation(
            original, root_message_id=1, revision=0, wall_time=1000, monotonic_time=60,
        )
        restored = decode_conversation(snapshot, wall_time=1001, monotonic_time=61)
        self.assertEqual(restored.reports["xlsx"], original.reports["xlsx"])

        data = json.loads(snapshot.payload)
        data["reports"][0]["sheets"][0]["issues"] = ["formula_like_cells:1"]
        with self.assertRaises(ValueError):
            decode_conversation(replace(snapshot, payload=json.dumps(data)), wall_time=1001)
        data = json.loads(snapshot.payload)
        data["reports"][0]["sheets"][0]["rows"][0].append("extra")
        with self.assertRaises(ValueError):
            decode_conversation(replace(snapshot, payload=json.dumps(data)), wall_time=1001)

    def test_text_import_roundtrip_and_source_scope(self):
        original = self.conversation()
        original.reports.clear()
        original.reports["text"] = TextImportArtifact(
            "text", 1, 100, 50, 60, "notes.md", "text/markdown", 8,
            "a" * 64, "2026-09-17T12:00:00+00:00", 1, "utf-8-sig",
            "markdown", "# Notes\n", 1,
        )
        snapshot = encode_conversation(original, root_message_id=1, revision=0,
                                       wall_time=1000, monotonic_time=60)
        restored = decode_conversation(snapshot, wall_time=1001, monotonic_time=61)
        self.assertEqual(restored.reports["text"], original.reports["text"])

        data = json.loads(snapshot.payload)
        data["reports"][0]["line_count"] = 2
        with self.assertRaises(ValueError):
            decode_conversation(
                replace(snapshot, payload=json.dumps(data)), wall_time=1001,
                monotonic_time=61,
            )

        data = json.loads(snapshot.payload)
        data["report_sources"]["text"] = [200]
        with self.assertRaisesRegex(ValueError, "provenance is incomplete"):
            decode_conversation(
                replace(snapshot, payload=json.dumps(data)), wall_time=1001,
                monotonic_time=61,
            )
