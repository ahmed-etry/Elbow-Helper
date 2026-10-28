"""Durable reply reports built from archived, structured request evidence."""

from datetime import datetime, timezone
import json

from elbow_helper.infrastructure.persistence import sqlite_transaction


def diagnostic_summary(record):
    """Keep observed arguments and accounting, without copying tool results."""
    return {
        "tool_calls": [{key: call[key] for key in ("capability", "arguments", "status")
                        if key in call} for call in record.get("tool_calls", ())],
        "rounds": [{key: round_[key] for key in ("phase", "round", "outcome", "usage")
                    if key in round_} for round_ in record.get("rounds", ())],
        **{key: record.get(key) for key in ("usage", "route", "status", "error", "terminal_cause")},
    }


def feedback_schema(connection):
    connection.execute("ALTER TABLE requests ADD COLUMN attachment_names_json TEXT NOT NULL DEFAULT '[]'")
    connection.execute("ALTER TABLE replies ADD COLUMN attachment_names_json TEXT NOT NULL DEFAULT '[]'")
    connection.execute("ALTER TABLE replies ADD COLUMN answer_content TEXT")
    # Old deliveries did not distinguish answers from confirmation-only previews.
    connection.execute("ALTER TABLE replies ADD COLUMN is_answer INTEGER NOT NULL DEFAULT 0")
    connection.execute("""CREATE TABLE request_diagnostics (
        request_message_id INTEGER PRIMARY KEY, summary_json TEXT NOT NULL
    )""")
    connection.execute("""CREATE TABLE reply_reports (
        report_id INTEGER PRIMARY KEY, guild_id INTEGER NOT NULL,
        channel_id INTEGER NOT NULL, reply_message_id INTEGER NOT NULL REFERENCES replies(message_id),
        request_message_id INTEGER NOT NULL REFERENCES requests(message_id),
        reporter_id INTEGER NOT NULL, reported_at TEXT NOT NULL, evidence_json TEXT NOT NULL,
        UNIQUE(reply_message_id, reporter_id)
    )""")
    connection.execute("CREATE INDEX reply_reports_request ON reply_reports(request_message_id)")
    for row in connection.execute("SELECT request_message_id, record_json FROM operator_debug_records ORDER BY record_id"):
        if row["request_message_id"] is not None:
            store_diagnostics(connection, row["request_message_id"], json.loads(row["record_json"]))


def store_diagnostics(connection, request_id, record):
    connection.execute("""INSERT INTO request_diagnostics VALUES (?, ?)
        ON CONFLICT(request_message_id) DO UPDATE SET summary_json=excluded.summary_json""",
        (request_id, json.dumps(diagnostic_summary(record), ensure_ascii=False, default=str)))


class ReplyReportArchiveMixin:
    def record_reply_delivery(self, *, request_id, reply_id, answer, attachment_names):
        """Retain a disclosed answer even when a combined message puts it in a file."""
        with self.connect() as connection, sqlite_transaction(connection, immediate=True):
            changed = connection.execute("""UPDATE replies SET answer_content=?, attachment_names_json=?
                WHERE message_id=? AND request_message_id=?""",
                (answer, json.dumps(list(attachment_names), ensure_ascii=False), reply_id, request_id))
            if changed.rowcount != 1:
                raise ValueError("Archived reply identity is unavailable for delivery metadata")

    def record_request_failure(self, *, request_id, error, terminal_cause):
        with self.connect() as connection, sqlite_transaction(connection, immediate=True):
            row = connection.execute("SELECT summary_json FROM request_diagnostics WHERE request_message_id=?",
                                     (request_id,)).fetchone()
            summary = json.loads(row[0]) if row is not None else diagnostic_summary({})
            summary.update(request_error=error, request_terminal_cause=terminal_cause)
            connection.execute("""INSERT INTO request_diagnostics VALUES (?, ?)
                ON CONFLICT(request_message_id) DO UPDATE SET summary_json=excluded.summary_json""",
                (request_id, json.dumps(summary, ensure_ascii=False)))

    def reply_request_id(self, *, guild_id, channel_id, reply_id):
        with self.connect() as connection:
            row = connection.execute("""SELECT r.request_message_id FROM replies r
                JOIN requests q ON q.message_id=r.request_message_id
                WHERE q.guild_id=? AND q.channel_id=? AND r.message_id=? AND r.is_answer=1""",
                (guild_id, channel_id, reply_id)).fetchone()
        return row[0] if row is not None else None

    def record_reply_report(self, *, guild_id, channel_id, reply_id, reporter_id, reported_at=None):
        with self.connect() as connection, sqlite_transaction(connection, immediate=True):
            request = connection.execute("""SELECT q.* FROM requests q JOIN replies r
                ON q.message_id=r.request_message_id
                WHERE q.guild_id=? AND q.channel_id=? AND r.message_id=? AND r.is_answer=1""",
                (guild_id, channel_id, reply_id)).fetchone()
            if request is None:
                return None
            if connection.execute("SELECT 1 FROM reply_reports WHERE reply_message_id=? AND reporter_id=?",
                                  (reply_id, reporter_id)).fetchone() is not None:
                return None
            request_id = request["message_id"]
            replies = [dict(row) for row in connection.execute("""SELECT message_id, COALESCE(answer_content, content) AS content,
                attachment_names_json FROM replies WHERE request_message_id=? AND is_answer=1
                ORDER BY message_id""", (request_id,))]
            diagnostics = connection.execute("SELECT summary_json FROM request_diagnostics WHERE request_message_id=?",
                                             (request_id,)).fetchone()
            evidence = {
                "question": request["content"],
                "question_attachment_names": json.loads(request["attachment_names_json"]),
                "delivered_answer": "\n".join(reply["content"] for reply in replies),
                "reply_ids": [reply["message_id"] for reply in replies],
                "answer_attachment_names": [name for reply in replies
                                            for name in json.loads(reply["attachment_names_json"])],
                "diagnostics": json.loads(diagnostics[0]) if diagnostics is not None else None,
            }
            reported_at = reported_at or datetime.now(timezone.utc).isoformat()
            cursor = connection.execute("""INSERT INTO reply_reports (guild_id, channel_id,
                reply_message_id, request_message_id, reporter_id, reported_at, evidence_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (guild_id, channel_id, reply_id, request_id, reporter_id, reported_at,
                 json.dumps(evidence, ensure_ascii=False)))
            return {"report_id": cursor.lastrowid, "guild_id": guild_id, "channel_id": channel_id,
                    "reply_message_id": reply_id, "request_message_id": request_id,
                    "reporter_id": reporter_id, "reported_at": reported_at, **evidence}
