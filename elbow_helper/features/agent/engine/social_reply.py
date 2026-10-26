"""Salvage social text and execute checked, bounded media outputs."""

import json
import logging
from dataclasses import replace

from elbow_helper.infrastructure.ai import AgentToolDefinition, AgentToolResult, TextGenerationError
from elbow_helper.infrastructure.ai.agent import AgentReasoningEffort
from ..access import require_evidence_access
from ..plan.checker import PlanCheck, check_plan, valid_arguments
from ..plan.results import result_handler
from ..research.member_provenance import member_target_issue
from ..wording import FAILURE_MESSAGE
from ..discord_actions.gif_library import ITEM_BY_ID, catalogue_text, channel_used_items
from ..discord_actions.meme_usage import recent_memes, select_meme
from ..discord_actions.gifs import gif_tools
from .rounds import AgentGracefulEnd
from .social_members import social_member_identity


LOGGER = logging.getLogger(__name__)
SOCIAL_REPLY_NAME = "submit_social_reply"
MEME_SEND_FIT = 4
SOCIAL_REPLY_INSTRUCTION = (
    "Call submit_social_reply for every social reply. First set mechanic_check by considering "
    "what any gameplay number measures: no_gameplay_number, possible, unclear, or impossible. "
    "Obvious comic exaggeration is not a factual gameplay number. "
    "Individual attack destruction uses whole percentages; aggregate war destruction may be decimal. "
    "For unclear mechanics, acknowledge the feeling without repeating or endorsing the number; "
    "for an impossible mechanic, give a brief player-like correction. Never invent a mechanic "
    "to sustain a bit. Set reaction_fit and reaction_basis by considering the chosen emojis "
    "as one expression in this exchange, using their supplied meanings. First ground every "
    "selected emoji in the same concrete point of the message. Then judge how they work together: "
    "each may express another aspect or strength of that response, without adding an unstated "
    "premise or switching subjects. The basis identifies that shared point and each emoji's "
    "contribution. Use a single emoji when it already expresses the whole response. "
    "When no fitting expression is clear, set reaction_fit=false, reaction_basis empty "
    "and reactions empty. Never stretch an emoji meaning to fill a reaction slot. "
    "A celebration reaction requires "
    "a celebratory line; never pair it with a jab that undercuts the win. "
    "Write the requested social reply in the persona's crude register, matching "
    "the requested intensity. Ground jabs in the supplied exchange, including when the target "
    "has no known history. The chat already supplies the setup; leave that recap "
    "out and start with your response. A direct greeting, agreement "
    "or celebration needs no extra joke. The grounding and reaction basis stay internal; "
    "they are not a recap to put in the reply. Keep text short and plain, without stacked jokes. "
    "Choose the reaction count for this message. "
    "Avoid every emoji from the previous bot reaction in this channel. A reaction never stands alone. "
    "Set gif_requested by judging whether the member is asking for a GIF or meme in context, "
    "including a short named meme or mood request. "
    "A passing mention or ordinary banter is not a request. "
    "Pick the meme first, the way a member reaches for a reaction GIF: shortlist the three library "
    "memes closest to this moment and rate how well each works as the reply on its own; the "
    "sarcastic or ironic uses listed for a meme count. A meme that fits well is sent alone and "
    "your line is only the fallback; reactions may accompany it. Then write your line. "
    "When the member asks for a GIF or meme, pick the closest library item. "
    "Text must stand alone and never mention or promise media. "
    "When asked to speak to, tease or answer another member, set address_member=true and "
    "member_query to that recipient. A request merely about someone keeps address_member=false. "
    "For a named third party, set member_query to their "
    "complete name as written in this request, keeping qualifiers even when unpunctuated. "
    "Resolve the complete label before media or prose; a shared first name is insufficient. Omit it for the asker "
    "or when no member is named. Identity resolution returns before any media runs; "
    "then compose for that member without borrowing facts about a namesake. "
    "Use empty reactions when unused. For facts, corrections or serious help, "
    "call load_research_context alone instead."
)


def social_reply_definition(registry, *, combo_due=False):
    forms = ["text"]
    if "react_to_request" in registry:
        forms.append("text_reactions")
        if combo_due:
            forms.append("text_reaction_combo")
    return AgentToolDefinition(
        SOCIAL_REPLY_NAME, "Respond to the social exchange in the persona's crude register. "
                           "A meme that lands is delivered alone; text is its standalone fallback.",
        {"type": "object", "properties": {
            "mechanic_check": {"type": "string", "enum": [
                "no_gameplay_number", "possible", "unclear", "impossible"],
                "description": "Check the stated mechanic before composing. Unclear: do not affirm the number. "
                               "Impossible: briefly correct the premise instead of playing along."},
            "reaction_fit": {"type": "boolean",
                             "description": "True when every selected emoji refers to the same point in the message and the complete expression fits."},
            "reaction_basis": {"type": "string", "minLength": 0, "maxLength": 240,
                               "description": "Internal reason identifying the shared point and each selected emoji's contribution to the response. Empty when unused."},
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
                                              "alone answers this message: 5 = a member here would post it as the reply, "
                                              "4 = it would work but a line is just as natural, 3 = related but off, 1-2 = wrong. "
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
                           "description": "Choose the reactions that go with the line. "
                               "A combination is optional when offered; "
                               "the form and reaction count follow this exchange alone."},
            "reactions": {"type": "array", "maxItems": 3 if combo_due else 1,
                          "description": "Empty for text; one for text_reactions; 2–3 distinct emojis for an offered text_reaction_combo. "
                                         "All share the text's mood. A celebration reaction requires a celebratory line, never a jab.",
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


async def social_advance(flow, *args, **kwargs):
    """Distinguish provider failures from an exhausted reply deadline."""
    try:
        return await flow.rounder.advance(*args, **kwargs)
    except TextGenerationError:
        flow.context.state.provider_failed = True
        flow.context.state.terminal_cause = 'provider_failed'
    except TimeoutError:
        flow.context.state.terminal_cause = 'timeout'
    except AgentGracefulEnd as error:
        # ModelRounds translates a timed-out provider call into a limit end.
        if not isinstance(error.__cause__, TimeoutError):
            raise
        flow.context.state.terminal_cause = 'timeout'
    await require_evidence_access(flow.context)
    return None


async def text_only_reply(flow, step, issue):
    """One low-effort round with no tools, acknowledging every pending call."""
    flow.session.replace_tools(())
    flow.rounder.expand_context = None
    feedback = {"error": issue}
    pending = tuple(AgentToolResult(call.call_id, json.dumps(feedback, ensure_ascii=False))
                    for call in step.tool_calls)
    reply = await social_advance(
        flow, pending, allow_tools=False, reasoning_effort=AgentReasoningEffort.LOW,
        max_output_tokens=1024, final=True,
        continuation_instruction=issue + " Reply to this message in one line. Return only text, no tools.",
    )
    await require_evidence_access(flow.context)
    if reply is None:
        return FAILURE_MESSAGE if flow.context.state.provider_failed else "That reply ran out of time."
    if reply.content.strip():
        return reply.content.strip()
    # Even a provider that ignores tool_choice may have supplied usable text.
    for call in reply.tool_calls:
        if call.name == SOCIAL_REPLY_NAME:
            try:
                choice = salvage_choice(json.loads(call.arguments), flow.registry,
                                        combo_due=flow.context.state.reaction_combo_due)
                if choice["text"]:
                    return choice["text"]
            except (TypeError, ValueError):
                continue
    return ""


async def read_social_reply(flow):
    """Resolve identity before delivering the graded meme and reactions."""
    pending = ()
    continuation = None
    identity_checked = False
    identity_query = None
    recipient = None
    for attempt in range(3):
        step = await social_advance(flow, pending, continuation_instruction=continuation)
        if step is None:
            return (FAILURE_MESSAGE if flow.context.state.provider_failed else "That reply ran out of time."), None
        if not flow.rounder.social_route:
            return None, step
        issue = SOCIAL_REPLY_INSTRUCTION
        plan = None
        if len(step.tool_calls) == 1 and step.tool_calls[0].name == SOCIAL_REPLY_NAME:
            call = step.tool_calls[0]
            try:
                choice = salvage_choice(json.loads(call.arguments), flow.registry,
                                        combo_due=flow.context.state.reaction_combo_due)
                tool, = gif_tools()
                registry = {**flow.registry, "find_gif": replace(tool, handler=result_handler(tool.handler))}
                plan = social_plan(choice, call.call_id, gif_available="find_gif" in registry,
                                   channel_bot_gifs=flow.context.state.channel_bot_gifs, context=flow.context)
                check = check_plan(plan, registry)
                if (identity_query is not None and choice.get("member_query")
                        and choice["member_query"].strip().casefold() != identity_query):
                    plan = None
                    raise ValueError("Keep the resolved member target for this social reply.")
                if check.ok and not identity_checked:
                    identity = await social_member_identity(flow, choice)
                    if identity is not None:
                        identity_checked = True
                        await require_evidence_access(flow.context)
                        if identity.get("status") == "needs_input":
                            return identity["missing"][0], None
                        if identity.get("status") != "resolved":
                            issue = "Member identity could not be resolved. Ask which member without IDs."
                        elif not attempt:
                            identity_query = choice["member_query"].strip().casefold()
                            if choice.get("address_member"):
                                recipient = identity["member"]["member_id"]
                            pending = (AgentToolResult(call.call_id, json.dumps(identity, ensure_ascii=False)),)
                            continuation = identity["instruction"] + " " + SOCIAL_REPLY_INSTRUCTION
                            continue
                        else:
                            issue = "Member identity needs a fresh reply. Ask which member without IDs."
                        plan = None
                        raise ValueError(issue)
                issue = check.error if not check.ok else member_target_issue(
                    flow.context, plan.get("reply", ""), self_target=plan["self_target"],
                )
            except (TypeError, ValueError) as error:
                issue = str(error)
        if flow.context.operator_debug is not None:
            for call in step.tool_calls:
                flow.context.operator_debug.checked(call.call_id, PlanCheck(not issue, issue))
        if not issue and plan is not None:
            flow.context.state.member_self_target = plan["self_target"]
            flow.context.state.gif_requested = choice["gif_requested"]
            if not plan.get("reply"):
                plan["reply"] = await text_only_reply(flow, step, "Supply the missing social text.")
                if member_target_issue(flow.context, plan["reply"]):
                    # Do not execute media for an unproven target; the final
                    # answer boundary can request an identity clarification.
                    return plan["reply"], None
            if plan["steps"]:
                previous_registry = flow.runner.registry
                flow.runner.registry = registry
                try:
                    results = await flow.runner.run(plan)
                finally:
                    flow.runner.registry = previous_registry
                flow.completed_results.update(results)
                flow.result_views.update(flow.runner.model_results_for(results))
                flow.model_results(results)
            await require_evidence_access(flow.context)
            sent_id = flow.context.state.gif_item_id if flow.context.state.gif_url else None
            LOGGER.info("Agent GIF choice: request=%s shortlist=%s sent=%s skipped_recent=%s",
                        getattr(flow.context.source_message, "id", None), choice["meme_shortlist"], sent_id,
                        flow.context.state.gif_skipped_recent)
            text = plan.get("reply", "")
            if recipient is not None and f"<@{recipient}>" not in text and f"<@!{recipient}>" not in text:
                text = f"<@{recipient}> {text}"
            if flow.context.state.gif_url:
                flow.context.state.gif_fallback_text = text
                return "", None
            # Media selection failed: the authored line remains the fallback.
            return text, None
        await require_evidence_access(flow.context)
        if not step.tool_calls and step.content.strip() and not member_target_issue(flow.context, step.content):
            return step.content.strip(), None
        return await text_only_reply(flow, step, issue), None
