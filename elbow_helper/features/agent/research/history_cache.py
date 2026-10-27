"""Reuse authorized raw history pages, checking the head for new messages."""

from dataclasses import replace
from elbow_helper.discord.message_search import DiscordMessageHistoryError

from ..read_cache import read_scope


HISTORY_PAGE_SECONDS = 5 * 60


async def history_page(context, *, channel_id, before_id, after_id, limit,
                       refresh_head=True):
    source = context.message_search
    cache = getattr(context, 'read_cache', None)
    async def fetch(before, after):
        return await source.history_page(channel_id=channel_id, before_id=before,
                                         after_id=after, limit=limit)
    if cache is None:
        return await fetch(before_id, after_id)
    prefix = ('history', read_scope(context), id(source), channel_id, after_id, limit)
    key = (*prefix, before_id)
    cached = None
    fetched_at = None
    if not context.fresh_reads:
        exact = cache.peek(key, max_age=HISTORY_PAGE_SECONDS)
        if exact is not None:
            cached = exact[0]
            fetched_at = cache.entries[key][2]
        else:
            # A later request has a different top boundary, but the old page is
            # still useful after reading the new head. Never join across a gap.
            for identity in reversed(tuple(cache.entries)):
                if identity[:-1] == prefix and identity[-1] <= before_id:
                    entry = cache.peek(identity, max_age=HISTORY_PAGE_SECONDS)
                    if entry is not None:
                        cached = entry[0]
                        fetched_at = cache.entries[identity][2]
                        break
    if cached is not None and cached.before_id == before_id and not refresh_head:
        return cached
    version = cache.begin(key)
    try:
        if cached is None or not cached.messages:
            page = await fetch(before_id, after_id)
            fetched_at = None
        else:
            newest = cached.messages[0].message_id
            delta = await fetch(before_id, newest)
            if (delta.before_id != before_id or delta.after_id != newest or delta.limit != limit
                    or delta.reached_window_start != (delta.next_before_id is None)
                    or delta.next_before_id is not None and not newest < delta.next_before_id < before_id
                    or len(delta.messages) > limit
                    or any(row.channel_id != channel_id or not newest < row.message_id < before_id
                           for row in delta.messages)):
                raise DiscordMessageHistoryError('Discord returned messages outside the requested history scope.')
            if delta.reached_window_start:
                rows = tuple({row.message_id: row for row in (*delta.messages, *cached.messages)}.values())
                rows = tuple(sorted(rows, key=lambda row: row.message_id, reverse=True))
                selected = rows[:limit]
                complete = cached.reached_window_start and len(rows) <= limit
                page = replace(cached, messages=selected, before_id=before_id, after_id=after_id,
                               next_before_id=None if complete else selected[-1].message_id,
                               reached_window_start=complete)
            else:
                page = replace(delta, after_id=after_id)
                fetched_at = None
        if not cache.is_current(key, version):
            # An edit/delete (or a newer read) raced this transport call. Do
            # not merge old cached rows or reinstall a superseded page.
            return await fetch(before_id, after_id)
        # Head checks and cursor hits never renew the age of cached old rows.
        if (page.before_id == before_id and page.after_id == after_id and page.limit == limit
                and page.reached_window_start == (page.next_before_id is None)
                and all(row.channel_id == channel_id and after_id < row.message_id < before_id
                        for row in page.messages)):
            cache.put(key, page, version=version, fetched_at=fetched_at)
        return page
    finally:
        cache.finish(key)
