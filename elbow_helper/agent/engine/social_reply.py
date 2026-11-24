"""Describe and validate banter replies and choose their media."""

from elbow_helper.infrastructure.ai import AgentToolDefinition

from ..plan.checker import valid_arguments
from ..discord_actions.gif_library import ITEM_BY_ID, catalogue_text, channel_used_items
from ..discord_actions.meme_usage import recent_memes, select_meme


SOCIAL_REPLY_NAME = "submit_social_reply"
MEME_SEND_FIT = 4


def social_reply_definition(registry, *, combo_due=False):
    forms = ["text"]
    if "react_to_request" in registry:
        forms.append("text_reactions")
        if combo_due:
            forms.append("text_reaction_combo")
    return AgentToolDefinition(
        SOCIAL_REPLY_NAME, "Respond to the social exchange in the persona's crude register. "
                           "A meme that lands is delivered alone, as your reply to the member you answer; "
                           "text is its standalone fallback. Reactions go on the member's message. "
                           "Your reply answers the asker's message unless you set a reply target; "
                           "to aim it at someone else, tag them (member_query) or reply to their message. "
                           "submit_social_reply ends the turn with your banter text or a library GIF.",
        {"type": "object", "properties": {
            "mechanic_check": {"type": "string", "enum": [
                "no_gameplay_number", "possible", "unclear", "impossible"],
                "description": "Check the stated mechanic before composing. Unclear: do not affirm the number. "
                               "Impossible: briefly correct the premise instead of playing along."},
            "reaction_fit": {"type": "boolean",
                             "description": "True when every selected emoji refers to the same point in the member's message and the complete expression fits."},
            "reaction_basis": {"type": "string", "minLength": 0, "maxLength": 240,
                               "description": "Internal reason naming the point in the member's message the emojis react to and each one's contribution. Empty when unused."},
            "gif_requested": {"type": "boolean",
                              "description": "True when the member asks for a GIF/meme in context, including "
                                             "a short named meme or mood request. False for ordinary banter. "
                                             "Requests use only the library."},
            "meme_shortlist": {"type": "array", "minItems": 3, "maxItems": 3, "uniqueItems": True,
                               "items": {"type": "object", "properties": {
                                   "id": {"type": "string", "enum": list(ITEM_BY_ID),
                                          "description": "A distinct library meme ID; do not repeat an ID in the shortlist."},
                                   "fit": {"type": "integer", "minimum": 1, "maximum": 5},
                               }, "required": ["id", "fit"], "additionalProperties": False},
                               "description": "The three library memes closest to this moment. Rate how well posting each one "
                                              "alone as your reply to the member answers this message: 5 = a member here would post it as the reply, "
                                              "4 = it fits and works as the reply, 3 = related but off, 1-2 = wrong. "
                                              "Items by group, as id = when members post it [what it shows]:\n"
                                              + catalogue_text()},
            "text": {"type": "string", "minLength": 1, "maxLength": 1800,
                     "description": "Your response to the exchange. Never open by repeating numbers, items or events "
                                    "from any member message, including earlier turns. "
                                    "The chat already supplies the setup; start with what you add. "
                                    "A new label for their action is still a recap. "
                                    "Direct greetings, agreement and celebration can be plain. "
                                    "Deliver a requested roast at its requested intensity, with natural swearing. "
                                    "Do not invent a target's real history, habits or gameplay. "
                                    "Share the mood of reactions and any GIF; celebrate a win without a jab. "
                                    "Sent only if no image is delivered."},
            "self_target": {"type": "boolean"},
            "address_member": {"type": "boolean",
                               "description": "True when this reply is directed to the named member, including an invited tease. False for a question about them."},
            "member_query": {"type": "string", "minLength": 1, "maxLength": 100,
                             "description": "Complete identity label from the request, including all supplied name parts and qualifiers."},
            # Combo availability changes these two; keeping them last lets the
            # static schema and meme catalogue before them stay in the prompt cache.
            "reply_form": {"type": "string", "enum": forms,
                           "description": "Choose the reactions that go on the member's message. "
                               "A combination is optional when offered; "
                               "the form and reaction count follow this exchange alone."},
            "reactions": {"type": "array", "maxItems": 3 if combo_due else 1,
                          "description": "Empty for text; one for text_reactions; 2–3 distinct emojis for an offered text_reaction_combo. "
                                         "A celebration reaction requires a celebratory line, never a jab.",
                          "items": {"type": "string", "minLength": 1, "maxLength": 100}},
        }, "required": ["mechanic_check", "reaction_fit", "reaction_basis", "reply_form", "gif_requested", "meme_shortlist", "text", "reactions"],
         "additionalProperties": False},
    )


def salvage_choice(choice, registry, *, combo_due=False):
    """Keep valid fields and shortlist entries independently."""
    schema = social_reply_definition(registry, combo_due=combo_due).parameters
    if not isinstance(choice, dict):
        raise ValueError("Supply a social reply object.")
    fields = {name: value for name, value in choice.items()
              if name != "meme_shortlist" and name in schema["properties"] and valid_arguments(
                  {name: value}, {"properties": {name: schema["properties"][name]}},
              )}
    shortlist = choice.get("meme_shortlist")
    fields["meme_shortlist"] = []
    seen = set()
    if isinstance(shortlist, list):
        for item in shortlist:
            if (valid_arguments(item, schema["properties"]["meme_shortlist"]["items"])
                    and item["id"] not in seen):
                fields["meme_shortlist"].append(dict(item))
                seen.add(item["id"])
    reactions = choice.get("reactions", [])
    reactions = [item for item in reactions if valid_arguments(
        {"emoji": item}, {"properties": {"emoji": schema["properties"]["reactions"]["items"]}},
    )][:3] if isinstance(reactions, list) and "react_to_request" in registry else []
    combo = (combo_due and choice.get("reply_form") == "text_reaction_combo"
             and 2 <= len(reactions) <= 3 and len(set(reactions)) == len(reactions))
    if not combo:
        reactions = reactions[:1]
    if (fields.get('reaction_fit') is not True
            or not fields.get('reaction_basis', '').strip()):
        reactions = []
        combo = False
    return {**fields, "text": fields.get("text", "").strip(),
            "gif_requested": fields.get("gif_requested", False),
            "reply_form": "text_reaction_combo" if combo else "text_reactions" if reactions else "text",
            "reactions": reactions}


def social_plan(choice, call_id, *, gif_available=False, channel_bot_gifs=None, context=None):
    """Convert salvaged fields into the existing checked GIF/reaction execution path."""
    text, reactions = choice["text"], choice["reactions"]
    send_fit = MEME_SEND_FIT + int(bool(channel_bot_gifs and channel_bot_gifs[-1]))
    meme, skipped = select_meme(
        choice["meme_shortlist"], requested=choice["gif_requested"], send_fit=send_fit,
        usage=recent_memes(context) if context is not None else (),
        excluded=channel_used_items(context) if context is not None else (),
    )
    if context is not None:
        context.state.gif_skipped_recent = skipped
    steps = []
    if gif_available and text and meme is not None:
        steps.append({"id": "social_gif_" + call_id[-24:], "capability": "find_gif",
                      "arguments": {"query": meme}, "depends_on": []})
    if reactions:
        steps.append({"id": "social_react_" + call_id[-24:], "capability": "react_to_request",
                      "arguments": {"emoji": reactions}, "depends_on": []})
    return {"goal": "Reply to the social exchange", "effort": "low", "output": "text",
            **({"reply": text} if text else {}),
            "self_target": choice.get("self_target", False), "steps": steps}
