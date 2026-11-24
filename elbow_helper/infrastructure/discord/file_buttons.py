"""Link buttons for files shared in Discord."""

import re

import discord


def add_file_buttons(
    view: discord.ui.View, *, google_link: str | None = None,
    attachment_url: str | None = None, row: int | None = None,
) -> bool:
    links = []
    if google_link:
        links.append(("Google Sheet", google_link))
        match = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", google_link)
        if match:
            attachment_url = (
                f"https://docs.google.com/spreadsheets/d/{match.group(1)}/export?format=xlsx"
            )
    if attachment_url:
        links.append(("Download", attachment_url))
    added = False
    for label, url in links:
        if not any(getattr(item, "url", None) == url for item in view.children):
            view.add_item(discord.ui.Button(
                label=label, style=discord.ButtonStyle.link, url=url, row=row,
            ))
            added = True
    return added
