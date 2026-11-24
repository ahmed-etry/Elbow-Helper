"""The SQL catalogue the loop's model reads: the declared datasets plus the reviewed additions."""
from __future__ import annotations

from functools import partial

from elbow_helper.agent.reads.sql.catalogue import SOURCES
from elbow_helper.agent.reads.sql.guide import RECRUITMENT, DataGuide
from elbow_helper.agent.reads.sql.query import query_context, reader_levels
from elbow_helper.agent.reads.sql.reviewed import (
    CLOSED, channel_sources, complete_source, current_roles_source,
)
from elbow_helper.agent.reads.sql.sources import DataSource
from elbow_helper.configuration.files import AGENT_DATABASE

from .names import ACCESS


class StoredData:
    def __init__(self, paths):
        raw_agent = DataSource("agent", paths.data_root / AGENT_DATABASE)
        self.sources = tuple(
            complete_source(paths, source) if not source.state_name and not source.snapshot else source
            for source in (*SOURCES, raw_agent)
        ) + channel_sources(paths) + (current_roles_source(),)
        self.guide = DataGuide(paths, self.sources)
        self.handler = partial(query_stored_data, sources=self.sources)

    def describe(self, context):
        """Return the shared catalogue, visible entries by access, and unavailable names."""
        levels = reader_levels(context)
        public = ["Family clans:", self.guide.clans]
        restricted = {}
        if "recruitment" in levels:
            restricted["recruitment"] = [RECRUITMENT]
        unavailable = {}
        closed = []
        for level, text in self.guide.entries:
            if level == CLOSED:
                closed.append(text.split(' ', 1)[0])
            elif level == "none":
                public.append(text)
            elif level in levels:
                context.state.required_access.add(level)
                restricted.setdefault(level, []).append(text.replace(f'[{level}]', f'[{ACCESS.get(level, level)}]', 1))
            else:
                unavailable.setdefault(level, []).append(text.split(" ", 1)[0])
        if closed:
            public.append('Closed to SQL: ' + ', '.join(closed) + '.')
        unavailable_text = (
            "Not available to this asker, by the access each needs: " + "; ".join(
                f"{ACCESS.get(level, level)}: {', '.join(names)}"
                for level, names in sorted(unavailable.items())
            ) + "."
            if unavailable else ""
        )
        return "\n".join(public), restricted, unavailable_text


async def query_stored_data(context, arguments, *, sources):
    """No max_rows reads the whole result; query_context applies the channel and role checks."""
    return await query_context(
        context, arguments["sql"], arguments.get("params"), arguments.get("max_rows"), sources=sources,
    )
