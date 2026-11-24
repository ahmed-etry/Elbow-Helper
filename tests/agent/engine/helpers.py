"""Script banter to exercise real reply handling without a model."""

from elbow_helper.agent.discord_actions.gif_library import ITEM_BY_ID
from elbow_helper.agent.engine.social_reply import MEME_SEND_FIT
from elbow_helper.infrastructure.ai import AgentStep, AgentUsage


def social_step(text="Synthetic reply.", *, form="text", meme_id=None, fit=MEME_SEND_FIT, reactions=(), call_id="social", **extra):
    """Script the required social output contract without a live model."""
    import json
    from elbow_helper.infrastructure.ai import AgentToolCall
    shortlist = [meme_id] if meme_id is not None else []
    shortlist.extend(item for item in ITEM_BY_ID if item != meme_id)
    return AgentStep("", (AgentToolCall(call_id, "submit_social_reply", json.dumps({
        "mechanic_check": "no_gameplay_number", "reply_form": form, "text": text,
        "reaction_fit": bool(reactions), "reaction_basis": "Synthetic semantic connection." if reactions else "",
        "gif_requested": False, "meme_shortlist": [
            {"id": item, "fit": fit if item == meme_id else 1} for item in shortlist[:3]],
        "reactions": list(reactions), **extra,
    })),), AgentUsage())
