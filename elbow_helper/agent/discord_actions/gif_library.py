"""Owner-approved memes, delivered as the clip the model named."""

import json
import logging
from importlib.resources import files
import re
from pathlib import Path

import discord

from elbow_helper.core.paths import ApplicationPaths
from ..models import AgentAttachment
from ..files.gif_media import provider_url

GROUPS = (
    "greeting", "goodbye", "agree", "refuse", "confused", "disbelief", "roast", "cringe",
    "laughing", "hype", "win", "loss", "sympathy", "angry", "shocked", "smug", "suspicious",
    "tired", "chaos",
)
LIBRARY = json.loads(files(__package__).joinpath("gif_library.json").read_text(encoding="utf-8"))
ITEM_BY_ID = {item["id"]: item for item in LIBRARY}
LOGGER = logging.getLogger(__name__)
MEDIA_EXTENSIONS = {".gif", ".png", ".jpg", ".webp"}


def catalogue_text():
    """List items under their group; the same bytes on every request keep the prompt cached."""
    lines = []
    for group in GROUPS:
        items = [item for item in ITEM_BY_ID.values() if item["group"] == group]
        if items:
            lines.append(group + ":")
            lines.extend(f"{item['id']} = {item['use']} [{item['shows']}]" for item in items)
    return "\n".join(lines)


def upload_limit(context):
    limit = getattr(getattr(context, "guild", None), "filesize_limit", None)
    return limit if isinstance(limit, int) and limit > 0 else discord.utils.DEFAULT_FILE_SIZE_LIMIT_BYTES


def library_directory(context):
    paths = getattr(context.bot, "paths", None) or ApplicationPaths.discover()
    return paths.data_root / "agent" / "gifs"


def _entry_key(entry):
    if not isinstance(entry, str):
        return None
    if Path(entry).name == entry and Path(entry).suffix.lower() in MEDIA_EXTENSIONS:
        return "attachment://" + entry
    try:
        if provider_url(entry):
            return entry
    except ValueError:
        pass
    return None


def _read_media(context, entry):
    try:
        directory = library_directory(context).resolve()
        path = (directory / entry).resolve()
        if path.parent != directory:
            raise ValueError("Media must stay inside the GIF library")
        limit = upload_limit(context)
        if path.stat().st_size > limit:
            raise ValueError("Media exceeds the Discord upload limit")
        with path.open("rb") as stream:
            data = stream.read(limit + 1)
        if not data or len(data) > limit:
            raise ValueError("Media is empty or exceeds the Discord upload limit")
        return AgentAttachment(entry, data)
    except (OSError, ValueError, RuntimeError) as error:
        LOGGER.warning("Skipping agent GIF library file %s: %s", entry, error)
        return None


def clip_family(item_id):
    """Clips of one joke share an id apart from a trailing _2, _3 and so on."""
    return re.sub(r"_(?:[2-9]|[1-9]\d+)$", "", item_id)


def library_gif(context, item_id):
    """Deliver the chosen item, an unused clip of its joke if it was already posted here, or nothing."""
    chosen = ITEM_BY_ID[item_id]
    seen = context.state.channel_gif_urls | context.state.used_gif_urls
    family = clip_family(item_id)
    clips = []
    if _entry_key(chosen["media"]) in seen:
        clips = [item for item in ITEM_BY_ID.values()
                 if item is not chosen and clip_family(item["id"]) == family
                 and _entry_key(item["media"]) not in seen]
        if context.state.gif_requested:
            clips.append(chosen)
    # Prefer unused clips; only an explicit request permits the chosen clip to repeat.
    for item in (clips if _entry_key(chosen["media"]) in seen else (chosen,)):
        media = item["media"]
        key = _entry_key(media)
        if key is None:
            continue
        if key.startswith("attachment://"):
            attachment = _read_media(context, media)
            if attachment is not None:
                context.state.gif_item_id = item["id"]
                return attachment
        else:
            context.state.gif_item_id = item["id"]
            return media
    return None


def channel_used_items(context):
    seen = context.state.channel_gif_urls | context.state.used_gif_urls
    return {item_id for item_id, item in ITEM_BY_ID.items() if _entry_key(item["media"]) in seen}


def channel_gif_urls(messages):
    """Reuse GIF URLs and attachment names from the authorized channel read."""
    from ..files.gif_media import gif_links
    urls = []
    for message in messages:
        try:
            urls.extend(gif_links(message))
        except ValueError:
            continue
    urls.extend(getattr(embed, "url", None) for message in messages
                for embed in getattr(message, "embeds", ()))
    seen = set(filter(None, urls))
    seen.update("attachment://" + item.filename for message in messages
                for item in getattr(message, "attachments", ())
                if Path(item.filename).suffix.lower() in MEDIA_EXTENSIONS)
    return seen
