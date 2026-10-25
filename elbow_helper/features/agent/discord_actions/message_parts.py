"""Render images without captions, preserving ordinary text-only messages."""

from pathlib import PurePath
from urllib.parse import urlsplit

from ..files.gif_media import URLS, provider_url
from ..files.images import EXTENSIONS
from ..text import chunk_response


def image_attachment(attachment):
    return attachment is not None and PurePath(attachment.filename).suffix.lower() in EXTENSIONS


def outgoing_parts(text, *, attachment=None):
    if image_attachment(attachment):
        return (("", True),)
    links = tuple(dict.fromkeys(url for raw in URLS.findall(text) if (
        provider_url(url := raw.rstrip(").,>"))
        or PurePath(urlsplit(url).path).suffix.lower() in EXTENSIONS)))
    if not links:
        return tuple((part, False) for part in chunk_response(text))
    return tuple((link, True) for link in links)
