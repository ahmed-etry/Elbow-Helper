"""Expose change details only to an authorized audience."""

from dataclasses import replace

from ..disclosure import can_disclose_provenance
from .contracts import PreparedAction


def detail_lines(action: PreparedAction) -> tuple[str, ...]:
    preview = action.preview
    if preview.details:
        return preview.details
    return preview.lines if preview.detail_sources or preview.detail_access else ()


async def prepare_preview(context) -> None:
    proposals = []
    for action in context.state.proposed_changes:
        preview = action.preview
        allowed = not (preview.detail_sources or preview.detail_access)
        if not allowed:
            allowed = await can_disclose_provenance(
                context, preview.detail_sources, preview.detail_access,
            )
        proposals.append(replace(action, details_hidden=not allowed))
    context.state.proposed_changes[:] = proposals
