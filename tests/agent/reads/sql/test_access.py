"""Each raw dataset keeps its feature's access policy through SQL routes."""

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from elbow_helper.configuration.roles import CO_LEADER_ROLE_ID, CORE_SENATE_ROLE_ID, ELDER_ROLE_ID, RECRUITER_ROLE_ID
from elbow_helper.infrastructure.persistence.documents import write_document

from elbow_helper.agent.reads.sql.catalogue import SOURCES
from elbow_helper.agent.features.role_connections.data import SOURCES as ROLE_SOURCES
from elbow_helper.agent.reads.sql.query import query_context, run_query
from elbow_helper.agent.reads.sql.reviewed import current_roles_source
from tests.agent.reads.dataset_helpers import synthetic_datasets
from tests.temp_directory import TemporaryDirectory


class SQLAccessTests(unittest.TestCase):
    def test_dataset_policies_and_public_earned_achievements(self):
        directory = self.enterContext(TemporaryDirectory())
        paths = synthetic_datasets(directory, only={"achievements", "state"})
        queries = [
            (SOURCES, f"SELECT count(*) AS n FROM achievements.{table}", "lead")
            for table in ("user_coins", "user_stats", "raffle_tickets")
        ]
        queries.extend((SOURCES, sql, "core") for sql in (
            "SELECT count(*) AS n FROM achievements.coin_transactions",
            'SELECT * FROM "achievements"."coin_transactions"',
            "SELECT * FROM coin_transactions",
            "SELECT a.* FROM achievements.coin_transactions a JOIN achievements.user_coins b ON 1=1",
        ))
        write_document(ROLE_SOURCES[0].resolve(paths), {"connections": []})
        queries.append((ROLE_SOURCES, "SELECT * FROM state.role_connections", "lead"))
        for sources, sql, required in queries:
            required_levels = {required}
            if " JOIN " in sql:
                required_levels.add("lead")
            for levels in (set(), {"lead_plus"}, {"core"}, {"lead"}, {"core", "lead"}):
                with self.subTest(sql=sql, levels=levels):
                    result, touched = run_query(sources, paths, levels, sql)
                    self.assertEqual("error" not in result, required_levels <= levels)
                    self.assertEqual(touched, required_levels if required_levels <= levels else set())
        result, touched = run_query(SOURCES, paths, set(),
            "SELECT count(*) AS n FROM achievements.user_achievements")
        self.assertNotIn("error", result)
        self.assertFalse(touched)


class MemberCommandsSQLTests(unittest.IsolatedAsyncioTestCase):
    async def test_role_query_reports_subject_commands_with_askers_help_visibility(self):
        def role(identifier, name):
            return SimpleNamespace(id=identifier, name=name, is_default=lambda: False)

        co = role(CO_LEADER_ROLE_ID, "Co-leader")
        recruiter = role(RECRUITER_ROLE_ID, "Recruiter")
        senate = role(CORE_SENATE_ROLE_ID, "Core Senate")
        elder = role(ELDER_ROLE_ID, "Elder")
        subject = SimpleNamespace(id=10, roles=[co, recruiter])
        asker = SimpleNamespace(id=20, roles=[senate, co])
        context = SimpleNamespace(
            guild=SimpleNamespace(roles=[co, recruiter, senate, elder], members=[subject, asker]),
            member=asker, bot=SimpleNamespace(paths=SimpleNamespace(data_root=Path.cwd())),
            state=SimpleNamespace(source_channels=set(), required_access=set()),
        )
        sources = (current_roles_source(),)
        sql = """SELECT member_id, group_concat(role_name, ' | ') AS discord_roles
                 FROM discord.member_roles WHERE member_id = :subject GROUP BY member_id"""
        with (patch("elbow_helper.agent.reads.sql.query.require_evidence_access"),
              patch("elbow_helper.agent.reads.sql.query.reader_levels", return_value=set())):
            result = await query_context(context, sql, {"subject": subject.id}, sources=sources)
            self.assertEqual(result["rows"], [{"member_id": subject.id, "discord_roles": "Co-leader | Recruiter"}])
            facts, = result["member_commands"]
            self.assertEqual(facts["additional_roles_for_reading"], ["Elder"])
            grants = {path: set(grant["through_roles"])
                      for grant in facts["commands"] for path in grant["paths"]}
            self.assertEqual(grants["/accept"], {"Co-leader", "Recruiter"})
            self.assertEqual(grants["/achievement award"], {"Co-leader"})
            self.assertEqual(grants["/hibernate"], {"Co-leader"})
            self.assertNotIn("/api", grants)

            reverse = await query_context(context, """SELECT member_id FROM discord.member_commands
                                                      WHERE command = '/accept' ORDER BY member_id""",
                                          sources=sources)
            self.assertEqual(reverse["rows"], [{"member_id": 10}, {"member_id": 20}])
            asker.roles = [recruiter]
            result = await query_context(context, sql, {"subject": subject.id}, sources=sources)
            self.assertFalse(any("/hibernate" in grant["paths"]
                                 for grant in result["member_commands"][0]["commands"]))
