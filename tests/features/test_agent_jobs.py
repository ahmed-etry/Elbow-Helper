from dataclasses import replace
from datetime import datetime, timezone
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import json
import sqlite3
import unittest
from unittest.mock import AsyncMock, patch

import discord

from elbow_helper.configuration.roles import CORE
from elbow_helper.discord.message_search import (
    DiscordHistoryPage, DiscordSearchMessage, DiscordSearchPage,
)
import elbow_helper.features.agent.research.repository as research_job_storage
from elbow_helper.features.agent.access import AgentAccessLost
from elbow_helper.features.agent.research.repository import (
    ResearchJobBusy, ResearchJobConflict, ResearchJobRepository,
    ResearchJobScope,
)
from elbow_helper.features.agent.research.contracts import ResearchJobDefinition
from elbow_helper.features.agent.research.runner import ResearchJobRunner
from elbow_helper.features.agent.models import AgentRequestContext, AgentTurnState
from elbow_helper.features.agent.reports.research import DiscordResearchReport
from elbow_helper.features.agent.tools.research import (
    cancel_discord_research_job,
    continue_discord_research_job,
    read_discord_research_job,
    read_discord_research_report,
    retain_discord_research_report,
    start_discord_history_job,
    start_discord_research_job,
)
from elbow_helper.features.agent.tools.research_batches import (
    list_discord_research_jobs, read_discord_research_jobs,
    start_discord_research_batch,
)


def _message(message_id=10):
    return {
        "message_id": message_id,
        "channel_id": 100,
        "channel": "lead-chat",
        "author_id": 42,
        "author": "Member",
        "timestamp": "2026-09-17T10:00:00+00:00",
        "content": "Decision evidence",
        "source": f"https://discord.com/channels/1/100/{message_id}",
    }


def _history_coverage(*, page_before=100, next_cursor=None, reached=True):
    return {
        "requested_after": "2026-09-16T00:00:00+00:00",
        "requested_before": "2026-09-17T00:00:00+00:00",
        "window_after_message_id": 1,
        "snapshot_before_message_id": 100,
        "page_before_message_id": page_before,
        "scanned_messages_in_window": 1,
        "returned_messages": 1,
        "next_cursor": next_cursor,
        "reached_requested_start": reached,
        "covers_currently_available_messages_only": True,
    }


class ResearchJobRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "jobs.sqlite3"
        self.repository = ResearchJobRepository(self.path)

    def create(self, *, now=1000):
        return self.repository.create(
            guild_id=1, conversation_root_id=2, requester_id=3,
            source_channel_id=100, query="decision", author_id=None,
            after=None, before="2026-09-17T12:00:00+00:00", page_size=10,
            now=now,
        )

    def create_history(self, *, now=1000):
        return self.repository.create(
            guild_id=1, conversation_root_id=2, requester_id=3,
            source_channel_id=100, query="", author_id=None,
            after="2026-09-16T00:00:00+00:00",
            before="2026-09-17T00:00:00+00:00", page_size=10,
            kind="history", now=now,
        )

    def test_checkpoint_reopens_with_cursor_messages_and_scope(self):
        created = self.create()
        scope = self.repository.scope(
            created.job_id, guild_id=1, conversation_root_id=2, now=1001,
        )
        self.assertEqual(scope.source_channel_id, 100)
        self.assertFalse(hasattr(scope, "messages"))
        claimed = self.repository.claim(
            created.job_id, guild_id=1, conversation_root_id=2,
            lease_owner="worker", now=1001,
        )
        checkpointed = self.repository.checkpoint_page(
            claimed, lease_owner="worker", page_messages=(_message(),),
            coverage={
                "next_cursor": "v1.10.20.hash", "total_results_estimate": 20,
                "deep_historical_indexing": False,
                "offset_limit_reached": False,
                "reached_current_indexed_end": False,
            },
            now=1002,
        )
        self.assertEqual(checkpointed.status, "partial")
        self.assertEqual(checkpointed.cursor, "v1.10.20.hash")
        self.assertEqual(checkpointed.messages, (_message(),))

        reopened = ResearchJobRepository(self.path).peek(
            created.job_id, guild_id=1, conversation_root_id=2, now=1003,
        )
        self.assertEqual(reopened, checkpointed)

    def test_create_many_is_atomic_and_restart_persistent(self):
        definitions = tuple(ResearchJobDefinition(
            source_channel_id=channel_id, query="decision", author_id=None,
            after=None, before="2026-09-17T12:00:00+00:00", page_size=10,
        ) for channel_id in (100, 200, 300))
        jobs = self.repository.create_many(
            guild_id=1, conversation_root_id=2, requester_id=3,
            definitions=definitions, now=1000,
        )
        self.assertEqual([job.source_channel_id for job in jobs], [100, 200, 300])
        reopened = ResearchJobRepository(self.path)
        self.assertEqual([
            reopened.peek(
                job.job_id, guild_id=1, conversation_root_id=2, now=1001,
            ).source_channel_id
            for job in jobs
        ], [100, 200, 300])
        with self.assertRaises(ValueError):
            self.repository.create_many(
                guild_id=1, conversation_root_id=2, requester_id=3,
                definitions=(definitions[0], definitions[0]), now=1002,
            )
        with self.repository.connect() as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM research_jobs",
            ).fetchone()[0]
        self.assertEqual(count, 3)

    def test_conversation_inventory_is_scoped_ordered_and_does_not_touch(self):
        older = self.create(now=1000)
        newer = self.repository.create(
            guild_id=1, conversation_root_id=2, requester_id=3,
            source_channel_id=200, query="decision", author_id=None,
            after=None, before="2026-09-17T12:00:00+00:00", page_size=10,
            now=1001,
        )
        scopes = self.repository.conversation_scopes(
            guild_id=1, conversation_root_id=2, now=1002,
        )
        self.assertEqual([scope.job_id for scope in scopes], [
            newer.job_id, older.job_id,
        ])
        self.assertEqual(self.repository.conversation_scopes(
            guild_id=1, conversation_root_id=99, now=1002,
        ), ())
        unchanged = self.repository.peek(
            older.job_id, guild_id=1, conversation_root_id=2, now=1002,
        )
        self.assertEqual(unchanged.updated_at, older.updated_at)
        self.assertEqual(unchanged.expires_at, older.expires_at)

    def test_cancel_many_checks_every_owner_before_mutation(self):
        definitions = tuple(ResearchJobDefinition(
            source_channel_id=channel_id, query="decision", author_id=None,
            after=None, before="2026-09-17T12:00:00+00:00", page_size=10,
        ) for channel_id in (100, 200))
        jobs = self.repository.create_many(
            guild_id=1, conversation_root_id=2, requester_id=3,
            definitions=definitions, now=1000,
        )
        with self.repository.connect() as connection:
            connection.execute(
                "UPDATE research_jobs SET requester_id=4 WHERE job_id=?",
                (jobs[1].job_id,),
            )
            connection.commit()
        with self.assertRaises(PermissionError):
            self.repository.cancel_many(
                tuple(job.job_id for job in jobs), guild_id=1,
                conversation_root_id=2, requester_id=3, now=1001,
            )
        with self.repository.connect() as connection:
            statuses = connection.execute(
                "SELECT status FROM research_jobs ORDER BY source_channel_id",
            ).fetchall()
        self.assertEqual([row["status"] for row in statuses], ["queued", "queued"])

    def test_live_lease_blocks_duplicate_worker_and_expired_lease_recovers(self):
        created = self.create()
        first = self.repository.claim(
            created.job_id, guild_id=1, conversation_root_id=2,
            lease_owner="first", now=1001,
        )
        with self.assertRaises(ResearchJobBusy):
            self.repository.claim(
                created.job_id, guild_id=1, conversation_root_id=2,
                lease_owner="second", now=1002,
            )
        recovered = self.repository.claim(
            created.job_id, guild_id=1, conversation_root_id=2,
            lease_owner="second", now=first.lease_expires_at,
        )
        self.assertEqual(recovered.lease_owner, "second")
        self.assertGreater(recovered.version, first.version)

    def test_expected_version_rejects_stale_continuation(self):
        created = self.create()
        with self.assertRaises(ResearchJobConflict):
            self.repository.claim(
                created.job_id, guild_id=1, conversation_root_id=2,
                lease_owner="worker", expected_version=created.version + 1,
                now=1001,
            )
        claimed = self.repository.claim(
            created.job_id, guild_id=1, conversation_root_id=2,
            lease_owner="worker", expected_version=created.version,
            now=1001,
        )
        self.assertEqual(claimed.status, "running")

    def test_stale_checkpoint_cannot_overwrite_newer_claim(self):
        created = self.create()
        first = self.repository.claim(
            created.job_id, guild_id=1, conversation_root_id=2,
            lease_owner="first", now=1001,
        )
        self.repository.claim(
            created.job_id, guild_id=1, conversation_root_id=2,
            lease_owner="second", now=first.lease_expires_at,
        )
        with self.assertRaises(ResearchJobConflict):
            self.repository.checkpoint_page(
                first, lease_owner="first", page_messages=(_message(),),
                coverage={
                    "next_cursor": None, "total_results_estimate": 1,
                    "deep_historical_indexing": False,
                    "offset_limit_reached": False,
                    "reached_current_indexed_end": True,
                }, now=first.lease_expires_at + 1,
            )

    def test_cancellation_wins_over_inflight_checkpoint_without_retaining_page(self):
        created = self.create()
        claimed = self.repository.claim(
            created.job_id, guild_id=1, conversation_root_id=2,
            lease_owner="worker", now=1001,
        )
        cancelled = self.repository.cancel(
            created.job_id, guild_id=1, conversation_root_id=2,
            requester_id=3, now=1002,
        )
        self.assertEqual(cancelled.status, "cancelled")
        settled = self.repository.checkpoint_page(
            claimed, lease_owner="worker", page_messages=(_message(),),
            coverage={
                "next_cursor": None, "total_results_estimate": 1,
                "deep_historical_indexing": False,
                "offset_limit_reached": False,
                "reached_current_indexed_end": True,
            }, now=1003,
        )
        self.assertEqual(settled.status, "cancelled")
        current = self.repository.peek(
            created.job_id, guild_id=1, conversation_root_id=2, now=1003,
        )
        self.assertEqual(current.messages, ())
        self.assertEqual(current.status, "cancelled")

    def test_owner_scope_expiry_and_pruning_are_enforced(self):
        created = self.create()
        self.assertIsNone(self.repository.scope(
            created.job_id, guild_id=2, conversation_root_id=2, now=1001,
        ))
        self.assertIsNone(self.repository.scope(
            created.job_id, guild_id=1, conversation_root_id=99, now=1001,
        ))
        with self.assertRaises(PermissionError):
            self.repository.cancel(
                created.job_id, guild_id=1, conversation_root_id=2,
                requester_id=4, now=1001,
            )
        self.assertIsNone(self.repository.peek(
            created.job_id, guild_id=1, conversation_root_id=2,
            now=created.expires_at,
        ))

        other = self.create(now=2000)
        self.assertEqual(self.repository.prune(now=other.expires_at), 1)

    def test_page_budget_stops_with_honest_partial_status(self):
        created = self.create()
        claimed = self.repository.claim(
            created.job_id, guild_id=1, conversation_root_id=2,
            lease_owner="worker", now=1001,
        )
        with patch("elbow_helper.features.agent.research.repository.MAX_JOB_PAGES", 1):
            result = self.repository.checkpoint_page(
                claimed, lease_owner="worker", page_messages=(_message(),),
                coverage={
                    "next_cursor": "v1.10.20.hash",
                    "total_results_estimate": 20,
                    "deep_historical_indexing": False,
                    "offset_limit_reached": False,
                    "reached_current_indexed_end": False,
                }, now=1002,
            )
        self.assertEqual(result.status, "partial")
        self.assertFalse(result.continuable)
        self.assertIsNone(result.cursor)

    def test_page_budget_also_stops_deep_index_waiting(self):
        created = self.create()
        claimed = self.repository.claim(
            created.job_id, guild_id=1, conversation_root_id=2,
            lease_owner="worker", now=1001,
        )
        with patch("elbow_helper.features.agent.research.repository.MAX_JOB_PAGES", 1):
            result = self.repository.checkpoint_page(
                claimed, lease_owner="worker", page_messages=(),
                coverage={
                    "next_cursor": None, "total_results_estimate": 20,
                    "deep_historical_indexing": True,
                    "offset_limit_reached": False,
                    "reached_current_indexed_end": False,
                }, now=1002,
            )
        self.assertEqual(result.status, "partial")
        self.assertFalse(result.continuable)

    def test_storage_budget_stops_without_committing_oversized_payload(self):
        created = self.create()
        claimed = self.repository.claim(
            created.job_id, guild_id=1, conversation_root_id=2,
            lease_owner="worker", now=1001,
        )
        with patch("elbow_helper.features.agent.research.repository.MAX_JOB_MESSAGES_BYTES", 100):
            result = self.repository.checkpoint_page(
                claimed, lease_owner="worker", page_messages=(_message(),),
                coverage={
                    "next_cursor": "v1.10.20.hash",
                    "total_results_estimate": 20,
                    "deep_historical_indexing": False,
                    "offset_limit_reached": False,
                    "reached_current_indexed_end": False,
                }, now=1002,
            )
        self.assertEqual(result.status, "partial")
        self.assertFalse(result.continuable)
        self.assertEqual(result.messages, ())
        self.assertTrue(result.coverage["retained_storage_limit_reached"])

    def test_final_page_overflow_is_partial_instead_of_claiming_completion(self):
        created = self.create()
        claimed = self.repository.claim(
            created.job_id, guild_id=1, conversation_root_id=2,
            lease_owner="worker", now=1001,
        )
        with patch("elbow_helper.features.agent.research.repository.MAX_JOB_MESSAGES", 1):
            result = self.repository.checkpoint_page(
                claimed, lease_owner="worker",
                page_messages=(_message(), _message(11)),
                coverage={
                    "next_cursor": None, "total_results_estimate": 2,
                    "deep_historical_indexing": False,
                    "offset_limit_reached": False,
                    "reached_current_indexed_end": True,
                }, now=1002,
            )
        self.assertEqual(result.status, "partial")
        self.assertFalse(result.continuable)
        self.assertEqual(len(result.messages), 1)
        self.assertTrue(result.coverage["retained_message_limit_reached"])

    def test_waiting_retry_deduplicates_messages_and_can_complete(self):
        created = self.create()
        first = self.repository.claim(
            created.job_id, guild_id=1, conversation_root_id=2,
            lease_owner="first", now=1001,
        )
        waiting = self.repository.checkpoint_page(
            first, lease_owner="first", page_messages=(_message(),),
            coverage={
                "next_cursor": None, "total_results_estimate": 2,
                "deep_historical_indexing": True,
                "offset_limit_reached": False,
                "reached_current_indexed_end": False,
            }, now=1002,
        )
        self.assertEqual(waiting.status, "waiting")
        self.assertTrue(waiting.continuable)

        second = self.repository.claim(
            created.job_id, guild_id=1, conversation_root_id=2,
            lease_owner="second", now=1003,
        )
        completed = self.repository.checkpoint_page(
            second, lease_owner="second",
            page_messages=(_message(), _message(11)),
            coverage={
                "next_cursor": None, "total_results_estimate": 2,
                "deep_historical_indexing": False,
                "offset_limit_reached": False,
                "reached_current_indexed_end": True,
            }, now=1004,
        )
        self.assertEqual(completed.status, "completed")
        self.assertEqual(
            [message["message_id"] for message in completed.messages], [10, 11],
        )

    def test_history_job_checkpoints_resume_and_complete(self):
        created = self.create_history()
        self.assertEqual(created.kind, "history")
        first = self.repository.claim(
            created.job_id, guild_id=1, conversation_root_id=2,
            lease_owner="first", now=1001,
        )
        partial = self.repository.checkpoint_page(
            first, lease_owner="first", page_messages=(_message(80),),
            coverage=_history_coverage(
                next_cursor="h1.50.100.scope", reached=False,
            ), now=1002,
        )
        self.assertEqual(partial.status, "partial")
        self.assertTrue(partial.continuable)
        self.assertIsNone(partial.total_results_estimate)

        second = self.repository.claim(
            created.job_id, guild_id=1, conversation_root_id=2,
            lease_owner="second", now=1003,
        )
        completed = self.repository.checkpoint_page(
            second, lease_owner="second", page_messages=(_message(40),),
            coverage=_history_coverage(page_before=50), now=1004,
        )
        self.assertEqual(completed.status, "completed")
        self.assertFalse(completed.continuable)
        self.assertEqual(len(completed.messages), 2)

    def test_repeated_history_cursor_stops_instead_of_looping(self):
        created = self.create_history()
        first = self.repository.claim(
            created.job_id, guild_id=1, conversation_root_id=2,
            lease_owner="first", now=1001,
        )
        cursor = "h1.50.100.scope"
        partial = self.repository.checkpoint_page(
            first, lease_owner="first", page_messages=(_message(80),),
            coverage=_history_coverage(next_cursor=cursor, reached=False),
            now=1002,
        )
        second = self.repository.claim(
            created.job_id, guild_id=1, conversation_root_id=2,
            lease_owner="second", now=1003,
        )
        stopped = self.repository.checkpoint_page(
            second, lease_owner="second", page_messages=(),
            coverage={
                **_history_coverage(
                    page_before=50, next_cursor=cursor, reached=False,
                ),
                "scanned_messages_in_window": 0,
                "returned_messages": 0,
            }, now=1004,
        )
        self.assertEqual(partial.cursor, cursor)
        self.assertEqual(stopped.status, "partial")
        self.assertFalse(stopped.continuable)
        self.assertTrue(stopped.coverage["repeated_cursor_detected"])

    def test_retryable_release_clears_lease_without_losing_checkpoint(self):
        created = self.create()
        claimed = self.repository.claim(
            created.job_id, guild_id=1, conversation_root_id=2,
            lease_owner="worker", now=1001,
        )
        self.repository.release_retryable(
            claimed, lease_owner="worker", error_class="search_unavailable",
            now=1002,
        )
        released = self.repository.peek(
            created.job_id, guild_id=1, conversation_root_id=2, now=1003,
        )
        self.assertEqual(released.status, "queued")
        self.assertEqual(released.error_class, "search_unavailable")
        self.assertIsNone(released.lease_owner)
        self.assertTrue(released.continuable)
        self.assertEqual(
            self.repository.claimable_scopes(guild_id=1, now=1020), (),
        )
        self.assertEqual(
            [item.job_id for item in self.repository.claimable_scopes(
                guild_id=1, now=1033,
            )],
            [created.job_id],
        )

    def test_claimable_scopes_recover_expired_leases_and_skip_cancelled_jobs(self):
        created = self.create()
        self.repository.claim(
            created.job_id, guild_id=1, conversation_root_id=2,
            lease_owner="lost-worker", now=1001,
        )
        self.assertEqual(
            self.repository.claimable_scopes(guild_id=1, now=1050), (),
        )
        recovered = self.repository.claimable_scopes(guild_id=1, now=1062)
        self.assertEqual([item.job_id for item in recovered], [created.job_id])

        self.repository.cancel(
            created.job_id, guild_id=1, conversation_root_id=2,
            requester_id=3, now=1063,
        )
        self.assertEqual(
            self.repository.claimable_scopes(guild_id=1, now=1064), (),
        )

    def test_malformed_persisted_content_and_newer_schema_fail_closed(self):
        created = self.create()
        with self.repository.connect() as connection, connection:
            connection.execute(
                "UPDATE research_jobs SET messages_json=? WHERE job_id=?",
                (json.dumps([{"content": "private"}]), created.job_id),
            )
        with self.assertRaises(ValueError):
            self.repository.peek(
                created.job_id, guild_id=1, conversation_root_id=2, now=1001,
            )

        with self.repository.connect() as connection, connection:
            connection.execute("PRAGMA user_version=3")
        with self.assertRaises(RuntimeError):
            ResearchJobRepository(self.path)

    def test_restored_history_rejects_reordered_or_wrong_author_rows(self):
        reordered = self.create_history()
        with self.repository.connect() as connection, connection:
            connection.execute(
                "UPDATE research_jobs SET messages_json=? WHERE job_id=?",
                (json.dumps([_message(40), _message(80)]), reordered.job_id),
            )
        with self.assertRaises(ValueError):
            self.repository.peek(
                reordered.job_id, guild_id=1, conversation_root_id=2,
                now=1001,
            )

        scoped = self.repository.create(
            guild_id=1, conversation_root_id=2, requester_id=3,
            source_channel_id=100, query="", author_id=99,
            after="2026-09-16T00:00:00+00:00",
            before="2026-09-17T00:00:00+00:00", page_size=10,
            kind="history", now=2000,
        )
        with self.repository.connect() as connection, connection:
            connection.execute(
                "UPDATE research_jobs SET messages_json=? WHERE job_id=?",
                (json.dumps([_message(40)]), scoped.job_id),
            )
        with self.assertRaises(ValueError):
            self.repository.peek(
                scoped.job_id, guild_id=1, conversation_root_id=2,
                now=2001,
            )

    def test_version_one_database_migrates_existing_jobs_to_search_kind(self):
        legacy = Path(self.directory.name) / "legacy.sqlite3"
        job_id = "a" * 32
        with closing(sqlite3.connect(legacy)) as connection, connection:
            research_job_storage._create_schema(connection)
            connection.execute(
                "INSERT INTO research_jobs VALUES ("
                "?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                "?, ?, ?, ?, ?, ?)",
                (
                    job_id, 1, 2, 3, 100, "decision", None, None,
                    "2026-09-17T12:00:00+00:00", 10, None, "queued", "[]",
                    0, None, None, 1, 0, None, 1000, 1000, 22_600, 0,
                    None, None,
                ),
            )
            connection.execute("PRAGMA user_version=1")
        migrated = ResearchJobRepository(legacy)
        restored = migrated.peek(
            job_id, guild_id=1, conversation_root_id=2, now=1001,
        )
        self.assertEqual(restored.kind, "search")
        self.assertEqual(restored.query, "decision")
        with migrated.connect() as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
        self.assertEqual(version, 2)


class _Channel:
    def __init__(self, channel_id=100, name="lead-chat"):
        self.id = channel_id
        self.name = name
        self.denied = set()
        self.guild = None

    def permissions_for(self, actor):
        allowed = actor.id not in self.denied
        return SimpleNamespace(
            view_channel=allowed, read_message_history=allowed,
        )


class ResearchJobToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.repository = ResearchJobRepository(
            Path(self.directory.name) / "jobs.sqlite3"
        )
        self.member = SimpleNamespace(
            id=10, roles=[SimpleNamespace(id=next(iter(CORE)))],
        )
        self.bot_member = SimpleNamespace(id=20)
        self.channel = _Channel()
        self.channels = {
            100: self.channel,
            200: _Channel(200, "cwl-chat"),
            300: _Channel(300, "planning-chat"),
        }
        guild = SimpleNamespace(
            id=1, me=self.bot_member,
            get_member=lambda value: self.member if value == self.member.id else None,
            get_channel_or_thread=self.channels.get,
        )
        self.guild = guild
        for channel in self.channels.values():
            channel.guild = guild
        self.search = SimpleNamespace(
            search_page=AsyncMock(return_value=DiscordSearchPage(
                (DiscordSearchMessage(
                    50, 100, 42, "Member", "Decision evidence",
                    "2026-09-17T10:00:00+00:00",
                ),),
                0, 10, 1, None, False, False,
            )),
            search=AsyncMock(return_value=()),
            history_page=AsyncMock(),
        )
        self.context = AgentRequestContext(
            bot=SimpleNamespace(fetch_channel=AsyncMock(return_value=None)),
            guild=guild, member=self.member,
            source_message=SimpleNamespace(
                id=1000,
                channel=self.channel,
                created_at=datetime(2026, 9, 17, 12, tzinfo=timezone.utc),
            ),
            account_links=None, clan_health=None, message_search=self.search,
            research_jobs=self.repository, conversation_root_id=500,
        )
        self.context.state.source_channels.add(100)

    def runner(self):
        bot = SimpleNamespace(
            get_guild=lambda value: self.guild if value == 1 else None,
            fetch_channel=AsyncMock(return_value=None),
        )
        return ResearchJobRunner(
            bot=bot, repository=self.repository,
            message_search=self.search, guild_id=1, poll_seconds=60,
        )

    async def test_runner_cursor_reaches_jobs_after_inaccessible_first_page(self):
        scopes = tuple(ResearchJobScope(
            f"{index:032x}", 1, 500, 42, 100, "queued",
        ) for index in range(1, 6))
        repository = SimpleNamespace()

        def claimable_scopes(*, after_job_id=None, **_):
            return tuple(
                scope for scope in scopes
                if after_job_id is None or scope.job_id > after_job_id
            )[:4]

        repository.claimable_scopes = claimable_scopes
        runner = ResearchJobRunner(
            bot=SimpleNamespace(), repository=repository,
            message_search=None, guild_id=1,
        )
        runner._context = AsyncMock(side_effect=lambda scope: (
            None if scope is not scopes[-1] else SimpleNamespace()
        ))
        with patch(
            "elbow_helper.features.agent.research.runner.advance_discord_research_job",
            new=AsyncMock(return_value={"status": "completed"}),
        ) as advance:
            self.assertEqual(await runner.run_once(), 0)
            self.assertEqual(await runner.run_once(), 1)
        advance.assert_awaited_once()
        self.assertEqual(advance.await_args.args[1], scopes[-1].job_id)

    async def start(self):
        return await start_discord_research_job(self.context, {
            "channel_id": 100, "query": "decision", "page_size": 10,
        })

    async def start_history(self):
        return await start_discord_history_job(self.context, {
            "channel_id": 100, "after": "2026-09-16", "page_size": 10,
        })

    async def completed_batch(self, channel_ids=(100, 200)):
        started = await start_discord_research_batch(self.context, {
            "channel_ids": list(channel_ids), "kind": "search",
            "query": "decision", "page_size": 10,
        })

        async def page(**kwargs):
            channel_id = kwargs["channel_ids"][0]
            return DiscordSearchPage(
                (DiscordSearchMessage(
                    1_000 + channel_id, channel_id, 42, "Member",
                    f"Decision evidence {channel_id}",
                    "2026-09-17T10:00:00+00:00",
                ),),
                0, 10, 1, None, False, False,
            )

        self.search.search_page.side_effect = page
        for job_id in started["job_ids"]:
            completed = await continue_discord_research_job(
                self.context, {"job_id": job_id},
            )
            self.assertEqual(completed["status"], "completed")
        return started

    async def test_job_runs_checkpoints_reopens_and_reads_without_background_work(self):
        started = await self.start()
        self.search.search_page.assert_not_awaited()
        advanced = await continue_discord_research_job(
            self.context, {"job_id": started["job_id"]},
        )
        self.assertEqual(advanced["status"], "completed")
        self.assertEqual(advanced["retained_message_count"], 1)
        self.assertEqual(advanced["new_messages"][0]["message_id"], 50)

        self.context = replace(
            self.context,
            research_jobs=ResearchJobRepository(self.repository.path),
        )
        read = await read_discord_research_job(
            self.context, {"job_id": started["job_id"]},
        )
        self.assertEqual(read["messages"][0]["content"], "Decision evidence")
        self.assertEqual(read["status"], "completed")
        call = self.search.search_page.await_args.kwargs
        self.assertEqual(call["channel_ids"], (100,))
        self.assertIsNotNone(call["max_id"])

    async def test_background_runner_advances_one_bounded_page_without_delivery(self):
        started = await self.start()
        advanced = await self.runner().run_once()
        self.assertEqual(advanced, 1)
        job = self.repository.peek(
            started["job_id"], guild_id=1, conversation_root_id=500,
        )
        self.assertEqual(job.status, "completed")
        self.assertEqual(job.pages_completed, 1)
        self.assertEqual(job.messages[0]["message_id"], 50)

    async def test_multi_channel_batch_creates_and_reads_independent_jobs(self):
        started = await start_discord_research_batch(self.context, {
            "channel_ids": [300, 100, 200], "kind": "search",
            "query": "decision", "page_size": 10,
        })
        self.assertEqual(started["job_count"], 3)
        self.assertEqual(
            [job["source_channel_id"] for job in started["jobs"]],
            [100, 200, 300],
        )
        self.assertEqual(self.context.state.source_channels, {100, 200, 300})
        self.search.search_page.assert_not_awaited()

        grouped = await read_discord_research_jobs(self.context, {
            "job_ids": started["job_ids"],
        })
        self.assertEqual(grouped["status_counts"], {"queued": 3})
        self.assertFalse(grouped["all_terminal"])
        self.assertEqual([job["version"] for job in grouped["jobs"]], [1, 1, 1])
        self.assertNotIn("messages", grouped["jobs"][0])

    async def test_research_inventory_recovers_ids_without_touching_or_leaking(self):
        started = await start_discord_research_job(self.context, {
            "channel_id": 200, "query": "decision", "page_size": 10,
        })
        before = self.repository.peek(
            started["job_id"], guild_id=1, conversation_root_id=500,
        )
        self.context = replace(
            self.context,
            research_jobs=ResearchJobRepository(self.repository.path),
            state=AgentTurnState(source_channels={100}),
        )
        listed = await list_discord_research_jobs(self.context, {})
        self.assertEqual(listed["jobs"], [{
            "job_id": started["job_id"], "status": "queued",
            "source_channel_id": 200, "requester_id": self.member.id,
        }])
        after = self.context.research_jobs.peek(
            started["job_id"], guild_id=1, conversation_root_id=500,
        )
        self.assertEqual((after.updated_at, after.expires_at, after.version), (
            before.updated_at, before.expires_at, before.version,
        ))

        self.context = replace(
            self.context, state=AgentTurnState(source_channels={100}),
        )
        self.channels[200].denied.add(self.member.id)
        hidden = await list_discord_research_jobs(self.context, {})
        self.assertEqual(hidden["jobs"], [])
        self.assertTrue(hidden["inaccessible_jobs_omitted"])

    async def test_multi_channel_batch_preflight_failure_creates_nothing(self):
        self.channels[200].denied.add(self.member.id)
        denied = await start_discord_research_batch(self.context, {
            "channel_ids": [100, 200], "kind": "search", "query": "decision",
        })
        self.assertIn("cannot access every", denied["error"])
        with self.repository.connect() as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM research_jobs",
            ).fetchone()[0]
        self.assertEqual(count, 0)

    async def test_multi_channel_batch_cancels_all_on_post_create_access_loss(self):
        with patch(
            "elbow_helper.features.agent.tools.research_batches.accessible_message_channel",
            AsyncMock(side_effect=(
                self.channels[100], self.channels[200],
                self.channels[100], None,
            )),
        ):
            with self.assertRaises(AgentAccessLost):
                await start_discord_research_batch(self.context, {
                    "channel_ids": [100, 200], "kind": "search",
                    "query": "decision",
                })
        with self.repository.connect() as connection:
            rows = connection.execute(
                "SELECT status, continuable FROM research_jobs ORDER BY source_channel_id",
            ).fetchall()
        self.assertEqual([tuple(row) for row in rows], [
            ("cancelled", 0), ("cancelled", 0),
        ])

    async def test_grouped_read_fails_closed_when_one_source_is_revoked(self):
        started = await start_discord_research_batch(self.context, {
            "channel_ids": [100, 200], "kind": "search", "query": "decision",
        })
        self.channels[200].denied.add(self.bot_member.id)
        with self.assertRaises(AgentAccessLost):
            await read_discord_research_jobs(self.context, {
                "job_ids": started["job_ids"],
            })
        jobs = [self.repository.peek(
            job_id, guild_id=1, conversation_root_id=500,
        ) for job_id in started["job_ids"]]
        self.assertEqual([job.version for job in jobs], [0, 0])

    async def test_background_runner_pauses_when_requester_loses_access(self):
        started = await self.start()
        self.member.roles = []
        advanced = await self.runner().run_once()
        self.assertEqual(advanced, 0)
        self.search.search_page.assert_not_awaited()
        job = self.repository.peek(
            started["job_id"], guild_id=1, conversation_root_id=500,
        )
        self.assertEqual(job.status, "queued")
        self.assertEqual(job.pages_completed, 0)

    async def test_completed_job_becomes_persistent_evidence(self):
        started = await self.start()
        advanced = await continue_discord_research_job(
            self.context, {"job_id": started["job_id"]},
        )
        self.assertEqual(advanced["status"], "completed")

        retained = await retain_discord_research_report(
            self.context, {"job_id": started["job_id"]},
        )
        report_id = retained["report_id"]
        report = self.context.state.reports[report_id]
        self.assertIsInstance(report, DiscordResearchReport)
        self.assertTrue(retained["coverage_complete"])
        self.assertEqual(retained["messages"][0]["content"], "Decision evidence")
        self.assertEqual(retained["limitations"], [])

        read = await read_discord_research_report(
            self.context, {"report_id": report_id},
        )
        self.assertEqual(read["source_job_id"], started["job_id"])

    async def test_continuing_job_cannot_be_retained_as_finished_evidence(self):
        started = await self.start()
        self.search.search_page.return_value = DiscordSearchPage(
            (DiscordSearchMessage(
                50, 100, 42, "Member", "First",
                "2026-09-17T10:00:00+00:00",
            ),),
            0, 10, 11, 10, False, False,
        )
        partial = await continue_discord_research_job(
            self.context, {"job_id": started["job_id"]},
        )
        self.assertTrue(partial["continuable"])
        retained = await retain_discord_research_report(
            self.context, {"job_id": started["job_id"]},
        )
        self.assertIn("Finish the research job", retained["error"])
        self.assertEqual(self.context.state.reports, {})

    async def test_bounded_terminal_partial_job_retains_explicit_limit(self):
        started = await self.start()
        self.search.search_page.return_value = DiscordSearchPage(
            (DiscordSearchMessage(
                50, 100, 42, "Member", "First",
                "2026-09-17T10:00:00+00:00",
            ),),
            0, 10, 11, 10, False, False,
        )
        with patch.object(research_job_storage, "MAX_JOB_PAGES", 1):
            partial = await continue_discord_research_job(
                self.context, {"job_id": started["job_id"]},
            )
        self.assertEqual(partial["status"], "partial")
        self.assertFalse(partial["continuable"])
        retained = await retain_discord_research_report(
            self.context, {"job_id": started["job_id"]},
        )
        self.assertFalse(retained["coverage_complete"])
        self.assertIn("terminal_partial_coverage", retained["limitations"])

    async def test_source_revocation_blocks_research_report_creation(self):
        started = await self.start()
        await continue_discord_research_job(
            self.context, {"job_id": started["job_id"]},
        )
        self.channel.denied.add(self.member.id)
        denied = await retain_discord_research_report(
            self.context, {"job_id": started["job_id"]},
        )
        self.assertIn("no longer accessible", denied["error"])
        self.assertEqual(self.context.state.reports, {})

    async def test_source_revocation_before_or_during_page_returns_no_content(self):
        started = await self.start()
        self.channel.denied.add(self.member.id)
        denied = await continue_discord_research_job(
            self.context, {"job_id": started["job_id"]},
        )
        self.assertIn("error", denied)
        self.search.search_page.assert_not_awaited()
        self.channel.denied.clear()

        async def revoke(**kwargs):
            self.channel.denied.add(self.bot_member.id)
            return DiscordSearchPage((), 0, 10, 1, None, False, False)

        self.search.search_page.side_effect = revoke
        denied = await continue_discord_research_job(
            self.context, {"job_id": started["job_id"]},
        )
        self.assertIn("error", denied)
        self.channel.denied.clear()
        job = self.repository.peek(
            started["job_id"], guild_id=1, conversation_root_id=500,
        )
        self.assertEqual(job.messages, ())
        self.assertEqual(job.status, "queued")

    async def test_core_role_revocation_blocks_read_and_continue(self):
        started = await self.start()
        self.member.roles = []
        with self.assertRaises(AgentAccessLost):
            await continue_discord_research_job(
                self.context, {"job_id": started["job_id"]},
            )
        with self.assertRaises(AgentAccessLost):
            await read_discord_research_job(
                self.context, {"job_id": started["job_id"]},
            )
        self.search.search_page.assert_not_awaited()

    async def test_role_loss_during_creation_cancels_the_job(self):
        access = AsyncMock(side_effect=(None, AgentAccessLost("role removed")))
        with patch(
            "elbow_helper.features.agent.tools.research.require_evidence_access",
            access,
        ):
            with self.assertRaises(AgentAccessLost):
                await self.start()
        with self.repository.connect() as connection:
            row = connection.execute(
                "SELECT status, continuable FROM research_jobs",
            ).fetchone()
        self.assertEqual(tuple(row), ("cancelled", 0))

    async def test_partial_cursor_resumes_after_repository_reopen(self):
        started = await self.start()
        self.search.search_page.side_effect = (
            DiscordSearchPage(
                (DiscordSearchMessage(
                    50, 100, 42, "Member", "First",
                    "2026-09-17T10:00:00+00:00",
                ),),
                0, 10, 11, 10, False, False,
            ),
            DiscordSearchPage(
                (DiscordSearchMessage(
                    40, 100, 42, "Member", "Second",
                    "2026-09-17T09:00:00+00:00",
                ),),
                10, 10, 11, None, False, False,
            ),
        )
        partial = await continue_discord_research_job(
            self.context, {"job_id": started["job_id"]},
        )
        self.assertEqual(partial["status"], "partial")
        self.assertTrue(partial["continuable"])

        self.context = replace(
            self.context,
            research_jobs=ResearchJobRepository(self.repository.path),
            source_message=SimpleNamespace(
                id=2000, channel=self.channel,
                created_at=datetime(2026, 9, 17, 13, tzinfo=timezone.utc),
            ),
        )
        completed = await continue_discord_research_job(
            self.context, {"job_id": started["job_id"]},
        )
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["retained_message_count"], 2)
        self.assertIsNotNone(
            self.search.search_page.await_args.kwargs.get("offset"),
        )
        self.assertEqual(self.search.search_page.await_args.kwargs["offset"], 10)

    async def test_history_job_resumes_cursor_after_repository_reopen(self):
        started = await self.start_history()
        self.assertEqual(started["kind"], "history")
        before = discord.utils.time_snowflake(
            datetime(2026, 9, 17, 12, tzinfo=timezone.utc),
        )
        after = discord.utils.time_snowflake(
            datetime(2026, 9, 16, tzinfo=timezone.utc),
        ) - 1
        first_id = before - 100
        self.search.history_page.side_effect = (
            DiscordHistoryPage(
                (DiscordSearchMessage(
                    first_id, 100, 42, "Member", "First",
                    "2026-09-17T10:00:00+00:00",
                ),), before, after, 10, first_id, False,
            ),
            DiscordHistoryPage((), first_id, after, 10, None, True),
        )
        partial = await continue_discord_research_job(
            self.context, {"job_id": started["job_id"]},
        )
        self.assertEqual(partial["status"], "partial")
        self.assertTrue(partial["continuable"])

        self.context = replace(
            self.context,
            research_jobs=ResearchJobRepository(self.repository.path),
        )
        completed = await continue_discord_research_job(
            self.context, {"job_id": started["job_id"]},
        )
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["retained_message_count"], 1)
        self.assertEqual(
            self.search.history_page.await_args.kwargs["before_id"], first_id,
        )

    async def test_history_job_keeps_cursor_when_author_page_has_no_matches(self):
        started = await start_discord_history_job(self.context, {
            "channel_id": 100, "after": "2026-09-16", "author_id": 99,
            "page_size": 10,
        })
        before = discord.utils.time_snowflake(
            datetime(2026, 9, 17, 12, tzinfo=timezone.utc),
        )
        after = discord.utils.time_snowflake(
            datetime(2026, 9, 16, tzinfo=timezone.utc),
        ) - 1
        message_id = before - 100
        self.search.history_page.return_value = DiscordHistoryPage(
            (DiscordSearchMessage(
                message_id, 100, 42, "Other", "Not by target",
                "2026-09-17T10:00:00+00:00",
            ),), before, after, 10, message_id, False,
        )
        partial = await continue_discord_research_job(
            self.context, {"job_id": started["job_id"]},
        )
        self.assertEqual(partial["status"], "partial")
        self.assertEqual(partial["retained_message_count"], 0)
        self.assertTrue(partial["continuable"])
        self.assertEqual(partial["coverage"]["scanned_messages_in_window"], 1)
        self.assertEqual(partial["coverage"]["returned_messages"], 0)

    async def test_history_job_releases_claim_when_source_changes_during_page(self):
        started = await self.start_history()
        before = discord.utils.time_snowflake(
            datetime(2026, 9, 17, 12, tzinfo=timezone.utc),
        )
        after = discord.utils.time_snowflake(
            datetime(2026, 9, 16, tzinfo=timezone.utc),
        ) - 1

        async def revoke(**kwargs):
            self.channel.denied.add(self.bot_member.id)
            return DiscordHistoryPage((), before, after, 10, None, True)

        self.search.history_page.side_effect = revoke
        denied = await continue_discord_research_job(
            self.context, {"job_id": started["job_id"]},
        )
        self.assertIn("error", denied)
        self.channel.denied.clear()
        job = self.repository.peek(
            started["job_id"], guild_id=1, conversation_root_id=500,
        )
        self.assertEqual(job.status, "queued")
        self.assertEqual(job.messages, ())

    async def test_job_is_conversation_scoped_and_only_creator_can_cancel(self):
        started = await self.start()
        self.context = replace(self.context, conversation_root_id=501)
        hidden = await read_discord_research_job(
            self.context, {"job_id": started["job_id"]},
        )
        self.assertIn("error", hidden)
        self.context = replace(self.context, conversation_root_id=500)

        other = SimpleNamespace(
            id=11, roles=[SimpleNamespace(id=next(iter(CORE)))],
        )
        self.context.guild.get_member = lambda value: (
            other if value == other.id else self.member if value == self.member.id else None
        )
        self.context = replace(self.context, member=other)
        denied = await cancel_discord_research_job(
            self.context, {"job_id": started["job_id"]},
        )
        self.assertIn("error", denied)

        self.context = replace(self.context, member=self.member)
        cancelled = await cancel_discord_research_job(
            self.context, {"job_id": started["job_id"]},
        )
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertFalse(cancelled["continuable"])

    async def test_stale_tool_version_does_not_claim_or_search(self):
        started = await self.start()
        result = await continue_discord_research_job(self.context, {
            "job_id": started["job_id"],
            "expected_version": started["version"] + 1,
        })
        self.assertIn("error", result)
        self.search.search_page.assert_not_awaited()
        job = self.repository.peek(
            started["job_id"], guild_id=1, conversation_root_id=500,
        )
        self.assertEqual(job.status, "queued")

    async def test_cancel_does_not_require_access_to_now_revoked_source(self):
        started = await self.start()
        self.channel.denied.add(self.member.id)
        cancelled = await cancel_discord_research_job(
            self.context, {"job_id": started["job_id"]},
        )
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertNotIn("messages", cancelled)


if __name__ == "__main__":
    unittest.main()
