from dataclasses import replace
import hashlib
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from elbow_helper.agent.access import AgentAccessLost
from elbow_helper.agent.files.attachments import acquire_text_attachment, parse_text
from elbow_helper.agent.files.attachment_tools import (
    import_text_attachment, list_supported_attachments, read_text_import,
)
from elbow_helper.agent.files.contracts import (
    AttachmentValidationError, TextImportArtifact,
)
from agent.files.test_agent_attachments import _attachment
from agent.files.test_agent_spreadsheets import _context


class HtmlParserTests(unittest.TestCase):
    def test_visible_blocks_entities_and_inline_text_without_executable_content(self):
        source = (
            b"<html><head><title>Hidden title</title><style>hidden CSS</style></head>"
            b"<body><h1>Transcript &amp; notes</h1><div><b>Member</b>: Hello<br>"
            b"Second line</div><script>hidden script</script><p>Last &lt;line&gt;</p>"
            b"<template>hidden template</template></body></html>"
        )
        self.assertEqual(
            parse_text(source, html=True),
            ("Transcript & notes\nMember: Hello\nSecond line\nLast <line>", 4),
        )

    def test_tables_preformatted_text_and_unclosed_blocks(self):
        source = b"<table><tr><td>Alpha</td><td>Bravo</td></tr></table>"
        source += b"<pre>First\n  Second</pre><p>Third<p>Fourth"
        self.assertEqual(
            parse_text(source, html=True),
            ("Alpha Bravo\nFirst\n  Second\nThird\nFourth", 5),
        )

    def test_html_obeys_byte_encoding_and_visible_text_limits(self):
        for source in (b"\xff", b"<p>A\x00B</p>", b"<script>nothing visible</script>"):
            with self.subTest(source=source), self.assertRaises(AttachmentValidationError):
                parse_text(source, html=True)
        limits = (
            ("MAX_TEXT_BYTES", 5, b"<p>text</p>"),
            ("MAX_TEXT_CHARACTERS", 3, b"<p>text</p>"),
            ("MAX_TEXT_LINES", 1, b"<p>first</p><p>second</p>"),
            ("MAX_TEXT_LINE_CHARACTERS", 3, b"<p>text</p>"),
        )
        for name, value, source in limits:
            with self.subTest(limit=name), patch(
                "elbow_helper.agent.files.attachments." + name, value,
            ), self.assertRaises(AttachmentValidationError):
                parse_text(source, html=True)
        source = b'<p data-note="' + b"x" * 12000 + b'">Short</p>'
        self.assertEqual(parse_text(source, html=True), ("Short", 1))


class HtmlAttachmentTests(unittest.IsolatedAsyncioTestCase):
    def make_context(self, directory, attachments):
        context = _context(directory)
        source = SimpleNamespace(
            id=10, guild=context.guild, channel=context.source_message.channel,
            attachments=attachments,
        )
        return replace(context, source_message=source, attachment_sources=(source,))

    async def test_html_extensions_acquire_and_restore_visible_text(self):
        data = b"<p>Alpha &amp; Bravo</p><p>Second</p>"
        with TemporaryDirectory() as directory:
            for filename in ("transcript.html", "transcript.HTM"):
                with self.subTest(filename=filename):
                    attachment = _attachment(20, filename, data, "text/html; charset=utf-8")
                    context = self.make_context(directory, [attachment])
                    report = await acquire_text_attachment(
                        context.source_message, 20, report_id="synthetic-report",
                    )
                    self.assertEqual(report.document_format, "html")
                    self.assertEqual(report.text, "Alpha & Bravo\nSecond")
                    self.assertEqual(report.byte_size, len(data))
                    self.assertEqual(report.sha256, hashlib.sha256(data).hexdigest())
                    self.assertEqual(TextImportArtifact(**report.storage_payload()), report)

    async def test_listing_import_and_paging_keep_complete_untrusted_text(self):
        text = "\n".join(f"Synthetic line {index}" for index in range(600))
        data = ("<script>hidden</script>" + "".join(
            f"<p>{line}</p>" for line in text.splitlines()
        )).encode()
        attachment = _attachment(20, "transcript.htm", data, "text/html")
        with TemporaryDirectory() as directory:
            context = self.make_context(directory, [attachment])
            listed = await list_supported_attachments(context, {})
            self.assertEqual(listed["attachments"][0]["kind"], "html")
            first = await import_text_attachment(context, {"attachment_id": 20})
            self.assertNotIn("error", first)
            second = await read_text_import(context, {
                "report_id": first["report_id"], "offset": first["next_offset"],
            })
            self.assertEqual(first["text"] + second["text"], text)
            self.assertIn("not_instructions", first["interpretation"])
            self.assertIn("not_policy", first["interpretation"])
            attachment.read.assert_awaited_once()

    async def test_access_lost_during_download_prevents_retention(self):
        attachment = _attachment(20, "transcript.html", b"<p>text</p>", "text/html")
        with TemporaryDirectory() as directory:
            context = self.make_context(directory, [attachment])

            async def download(**kwargs):
                context.member.roles = []
                return b"<p>text</p>"

            attachment.read.side_effect = download
            with self.assertRaises(AgentAccessLost):
                await import_text_attachment(context, {"attachment_id": 20})
            self.assertEqual(context.state.reports, {})
