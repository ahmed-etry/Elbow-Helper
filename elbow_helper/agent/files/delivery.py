"""Present prepared workbooks with the feature export links or their fallback files."""

from __future__ import annotations

from dataclasses import replace
import io
import discord

from elbow_helper.agent.safety.access import require_evidence_access
from elbow_helper.infrastructure.discord.file_buttons import add_file_buttons
from .workbooks import publish_workbook_bytes


def delivery_manifest(attachments):
    """Describe retained outputs independently of paged or bounded read evidence."""
    files = []
    for item in attachments:
        pending = ((item.report_id or "").startswith("spreadsheet:")
                   and not item.publication_attempted and not item.google_link)
        entry = {"filename": item.filename,
                 "delivery": "link" if item.google_link else "publish_or_attach" if pending else "attachment"}
        if item.google_link:
            entry["url"] = item.google_link
        if item.total_rows is not None:
            entry["total_rows"] = item.total_rows
        if item.sheet_rows:
            entry["sheet_count"] = len(item.sheet_rows)
            entry["sheets"] = [{"name": name, "rows": count} for name, count in item.sheet_rows]
        files.append(entry)
    return {"files": files}


async def publish_spreadsheet(context, attachments):
    """Publish the retained agent workbook once, immediately before assembling its reply."""
    prepared = []
    for item in attachments:
        if ((item.report_id or "").startswith("spreadsheet:")
                and not item.publication_attempted):
            await require_evidence_access(context)
            link, warning = await publish_workbook_bytes(
                context.bot, item.data, item.spreadsheet_title,
            )
            updated = replace(item, google_link=link, google_warning=warning,
                              publication_attempted=True)
            for index, stored in enumerate(context.state.attachments):
                if stored is item:
                    context.state.attachments[index] = updated
            await require_evidence_access(context)
            item = updated
        prepared.append(item)
    return prepared


class SpreadsheetLinksView(discord.ui.View):
    """Link-only controls need no expiry or callback registration."""

    def __init__(self):
        super().__init__(timeout=None)
        self.message = None
        self.expired = True


def attachment_files(attachments):
    return [discord.File(io.BytesIO(item.data), filename=item.filename)
            for item in attachments if not item.google_link]


def spreadsheet_warnings(attachments, parts=()):
    return tuple(dict.fromkeys(
        item.google_warning for item in attachments
        if not item.google_link and item.google_warning
        and not any(item.google_warning in part for part in parts)
    ))


def spreadsheet_response(response, attachments):
    return "\n\n".join(filter(None, (response, *spreadsheet_warnings(attachments, (response,)))))


def spreadsheet_links(view, attachments):
    for row, item in enumerate(item for item in attachments if item.google_link):
        if view is None:
            view = SpreadsheetLinksView()
        add_file_buttons(view, google_link=item.google_link, row=row)
    return view


def has_spreadsheet_download(attachments):
    return any(not item.google_link and item.filename.lower().endswith(".xlsx")
               for item in attachments)


async def add_spreadsheet_downloads(message, view, attachments):
    filenames = {item.filename for item in attachments
                 if not item.google_link and item.filename.lower().endswith(".xlsx")}
    if not filenames:
        return message
    added = False
    for attachment in getattr(message, "attachments", ()):
        if attachment.filename in filenames:
            if view is None:
                view = SpreadsheetLinksView()
            added |= add_file_buttons(view, attachment_url=attachment.url)
    if added:
        return await message.edit(view=view)
    return message
