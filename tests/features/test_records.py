from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from elbow_helper.configuration.roles import LEAD_PLUS
from elbow_helper.features.records.commands import RecordCommandMixin
from elbow_helper.features.records.database import RecordRepository
from elbow_helper.features.records.service import RecordService


class _AccountLinks:
    def get_links_for_user(self, user_id: int):
        return [{"discord_user_id": user_id, "player_tag": "#PLAYER"}]


class _RecordReader:
    def __init__(self, records: list[dict] | None = None) -> None:
        self.records = records or []
        self.calls = 0

    def list(self, **_kwargs):
        self.calls += 1
        return self.records


class RecordServiceTests(unittest.TestCase):
    def test_create_edit_remove_and_read_contract(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            repository = RecordRepository(
                Path(temporary_directory) / "records.sqlite3"
            )
            repository.initialize()
            service = RecordService(repository, _AccountLinks())
            member = SimpleNamespace(
                id=10,
                display_name="Member",
                name="member",
            )
            actor = SimpleNamespace(
                id=20,
                display_name="Lead",
                name="lead",
            )

            created = service.create(
                member=member,
                category_key="war",
                incident_type_key="war_missed_attacks",
                note="Missed both attacks.",
                recorder=actor,
            )
            self.assertEqual(created["status"], "active")
            self.assertEqual(
                service.reader.active_for_members([10])[0]["id"],
                created["id"],
            )

            edited = service.edit(
                record_id=created["id"],
                member_id=member.id,
                category_key="communication",
                incident_type_key="communication_no_response",
                note="Did not respond to follow-up.",
                editor=actor,
            )
            self.assertIsNotNone(edited)
            self.assertEqual(
                edited["incident_type_key"],
                "communication_no_response",
            )

            removed = service.remove(
                record_id=created["id"],
                member_id=member.id,
                remover=actor,
            )
            self.assertIsNotNone(removed)
            self.assertEqual(service.reader.list(member_id=10), [])
            self.assertEqual(
                service.reader.list(
                    member_id=10,
                    include_removed=True,
                )[0]["status"],
                "removed",
            )


class RecordAutocompleteTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _interaction(*, role_ids: tuple[int, ...] = ()) -> SimpleNamespace:
        return SimpleNamespace(
            user=SimpleNamespace(
                roles=[SimpleNamespace(id=role_id) for role_id in role_ids]
            ),
            namespace=SimpleNamespace(user=SimpleNamespace(id=10)),
        )

    async def test_unauthorized_autocomplete_does_not_read_records(self) -> None:
        records = _RecordReader(
            [
                {
                    "id": 1,
                    "created_ts": 1_700_000_000,
                    "incident_type_key": "war_missed_attacks",
                }
            ]
        )
        commands = RecordCommandMixin()
        commands.reader = records

        choices = await commands.record_autocomplete(
            self._interaction(),
            "",
        )

        self.assertEqual(choices, [])
        self.assertEqual(records.calls, 0)

    async def test_authorized_autocomplete_returns_records(self) -> None:
        records = _RecordReader(
            [
                {
                    "id": 1,
                    "created_ts": 1_700_000_000,
                    "incident_type_key": "war_missed_attacks",
                }
            ]
        )
        commands = RecordCommandMixin()
        commands.reader = records

        choices = await commands.record_autocomplete(
            self._interaction(role_ids=(next(iter(LEAD_PLUS)),)),
            "",
        )

        self.assertEqual(records.calls, 1)
        self.assertEqual(len(choices), 1)
        self.assertEqual(choices[0].value, "1")
        self.assertIn("Missed Attack", choices[0].name)
