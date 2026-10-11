"""Retain only found GIFs that have not been assigned to another plan step."""

from ..plan.checker import result_references
from ..plan.executor import resolve_arguments, UnresolvedReferenceError

MESSAGE_CAPABILITIES = {"post_discord_message", "edit_agent_message", "send_direct_messages"}


def consume_gifs(state, steps, results):
    found = {identity: results[identity]["url"] for identity, step in steps.items()
             if step["capability"] == "find_gif" and identity in results
             and results[identity].get("url")}
    for identity, step in steps.items():
        result = results.get(identity, {})
        if "error" in result or result.get("flags", {}).get("status") in ("failed", "refused"):
            continue
        for reference in result_references(step["arguments"]):
            if reference["step"] in found:
                state.used_gif_urls.add(found[reference["step"]])
        if step["capability"] in MESSAGE_CAPABILITIES:
            try:
                text = resolve_arguments(step["arguments"], results).get("text", "")
            except UnresolvedReferenceError:
                continue
            if isinstance(text, str):
                state.used_gif_urls.update(url for url in found.values() if url in text)
    if state.gif_url in state.used_gif_urls:
        state.gif_url = None
