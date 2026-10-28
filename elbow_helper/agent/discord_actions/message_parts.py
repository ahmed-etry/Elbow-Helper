"""Send requested text whole; only an image file is posted without a caption."""

from pathlib import PurePath
from urllib.parse import urlsplit

from ..files.gif_media import URLS, provider_url
from ..files.images import EXTENSIONS
from ..text import chunk_response


def image_attachment(attachment):
    return attachment is not None and PurePath(attachment.filename).suffix.lower() in EXTENSIONS


def media_links(text):
    return tuple(dict.fromkeys(url for raw in URLS.findall(text) if (
        provider_url(url := raw.rstrip(").,>"))
        or PurePath(urlsplit(url).path).suffix.lower() in EXTENSIONS)))


def outgoing_parts(text, *, attachment=None):
    # GIF and image links stay inside the text; Discord previews them under it.
    return ("",) if image_attachment(attachment) else tuple(chunk_response(text))
