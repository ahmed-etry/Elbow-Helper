"""Separate provider GIF links from text without changing ordinary messages."""

import re
from types import SimpleNamespace

from ..files.gif_media import gif_links
from ..text import chunk_response


def outgoing_parts(text):
    links = tuple(dict.fromkeys(gif_links(SimpleNamespace(content=text))))
    if not links:
        return tuple((part, False) for part in chunk_response(text))
    body = text
    for link in links:
        body = re.sub(r"\[([^\]]*)\]\(" + re.escape(link) + r"\)", r"\1", body)
        body = body.replace("<" + link + ">", "").replace(link, "")
    body = body.strip()
    return (*((part, False) for part in chunk_response(body)),
            *((link, True) for link in links))
