"""Present prepared workbooks with the feature export links or their fallback files."""

from __future__ import annotations

import io
import re

import discord


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
        label = item.spreadsheet_title if len(published) > 1 else "Google Sheet"
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
