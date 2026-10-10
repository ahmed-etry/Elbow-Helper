"""Present prepared workbooks with the feature export links or their fallback files."""

from __future__ import annotations

from dataclasses import replace
import io
import re

import discord

from ..access import require_evidence_access
from .workbooks import publish_workbook_bytes


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
    published = [item for item in attachments if item.google_link]
    if not published:
        return view
    view = view if view is not None else SpreadsheetLinksView()
    for index, item in enumerate(published, 1):
        match = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", item.google_link)
        label = "Google Sheet"
        links = [(label, item.google_link)]
        if match:
            links.append(("Download", (
                f"https://docs.google.com/spreadsheets/d/{match.group(1)}/export?format=xlsx"
            )))
        for label, url in links:
            if not any(getattr(button, "url", None) == url for button in view.children):
                view.add_item(discord.ui.Button(
                    label=label, style=discord.ButtonStyle.link, url=url, row=index,
                ))
    return view
