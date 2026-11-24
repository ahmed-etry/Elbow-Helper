"""One model conversation over the bot's authorized read tools."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import asdict, replace
from hashlib import sha256
import json
import logging
import time

from elbow_helper.agent.actions.contracts import ActionClass, ActionRefused, PreparedAction, check_bundle
from elbow_helper.agent.actions.details import prepare_preview
from elbow_helper.agent.actions.outcomes import command_reply
from elbow_helper.agent.actions.preview import preview_text
from elbow_helper.agent.commands.bridge import build_command_tools
from elbow_helper.agent.conversation.debug import OperatorDebugRecord
from elbow_helper.agent.conversation.preparation import prepare_history
from elbow_helper.agent.conversation.wording import ACTION_UNAVAILABLE, AGENT_ANSWER_UNFINISHED, AGENT_PRIVATE_CHECK_BUTTON
from elbow_helper.agent.discord_actions.meme_usage import recent_meme_context
from elbow_helper.agent.discord_actions.permission_checks import asker_permissions
from elbow_helper.agent.discord_actions.reaction_pacing import combo_slot_status, reply_combo_due
from elbow_helper.agent.discord_actions.reactions import recent_reaction_context, server_reaction_context
from elbow_helper.agent.discord_actions.roles import role_change_access
from elbow_helper.agent.engine import budgets as limits
from elbow_helper.agent.engine.adapters import enabled_adapters
from elbow_helper.agent.engine.capability_contract import CapabilityBindError, compile_capability_call
from elbow_helper.agent.engine.member_usage import MemberUsageLimit, MemberUsageLimiter
from elbow_helper.agent.engine.models import AgentCapabilityEffect, RegisteredAgentTool
from elbow_helper.agent.engine.prompts import CONTINUE_ANSWER_INSTRUCTION, REPEAT_TOOL_CALL_INSTRUCTION
from elbow_helper.agent.engine.registry import build_agent_tools
from elbow_helper.agent.engine.social_reply import SOCIAL_REPLY_NAME
from elbow_helper.agent.engine.tool_call import (
    AgentUnavailableError, execute_tool, restore_tool_state, tool_state_snapshot,
)
from elbow_helper.agent.engine.usage import RequestUsage
from .lookup_access import disclosure_issue
from .names import PERMISSIONS
from elbow_helper.agent.files.images import request_images
from elbow_helper.agent.knowledge.answers import private_answer_pass, private_check_available
from elbow_helper.agent.knowledge.tools import _section_id, channel_knowledge_access
from elbow_helper.agent.plan.checker import argument_issue
from elbow_helper.agent.reads.snapshots import SLICE_COLUMNS
from elbow_helper.agent.reports.tools import original_tool
from elbow_helper.agent.safety.access import AgentAccessLost, leadership_asker, require_access, require_evidence_access
from elbow_helper.agent.safety.disclosure import log_scope, request_scope
from elbow_helper.infrastructure.ai import TextGenerationError
from elbow_helper.infrastructure.ai.agent import AgentReasoningEffort, AgentToolDefinition, AgentToolResult

from elbow_helper.agent.conversation.identity import guild_identity

from .banter import GIF_NAME, Banter, banter_definition
from .community_map import load_meanings
from .prior import last_result_id, prior_messages, resolve_reference
from .results import row_pages, shape
from .structure import Server, Structure
from .tools import model_tools
from .wording import BANTER_SYSTEM, COMBO_AVAILABLE

LOGGER = logging.getLogger(__name__)

SYSTEM_OPENING = """You are Elbow Helper, an AI participant in the Brown Elbow Clash of Clans Discord community.
Talk like a blunt regular in clan chat: short, plain, rude when it fits. Swearing is normal speech. Help and data questions get the useful answer without a trailing jab. Let the task set the tone. Refer to members by their server nickname, the supplied display name.
If you can't tell what someone means, ask them what they mean instead of guessing.
Discord messages do not render Markdown tables.
Use only information available to both the requester and this channel.
Look up live Clash profiles, clans, rosters and wars, along with stored accounts, membership, activity, war, CWL and battle records. Read Discord members, channels, threads and messages, plus bot workflows, reports, attachments and command help. Search approved knowledge for other community notes. Use query_bot_data for read-only SQLite queries.
Changes and commands you prepare appear on a Confirm card the asker sees before they run. Include any refusal, question or needed warning in your reply.
react_to_request puts one emoji on the request. submit_social_reply is the reply for banter: it sends the line, a GIF from the library and reactions, so look up what you need before calling it. set_discord_reply_target chooses which message your reply answers. Reaction and GIF pacing is automatic.
Give no army, attack or base strategy of your own; you have no reliable source for it. Point to where the community gets it, as the notes describe: commands, channels and Coaches. When a command or channel from the notes fits what a member is trying to do and you can't do it yourself, mention it."""

REFUSED_CHANGE_NOTE = ("Nothing was posted or changed, and the text you drafted reached no one. "
                       "Your next message is the reply to the asker.")
PAGE_BYTES = 16 * 1024
PAGE_TOOL = AgentToolDefinition(
    "read_result_page", "Results over 16 KB come in pages; read_result_page(result_id, page). "
    "A result with kept:false can't be reused by report_id; read its pages now through read_result_page.",
    {"type": "object", "properties": {
        "result_id": {"type": "string"}, "page": {"type": "integer", "minimum": 1},
    }, "required": ["result_id", "page"], "additionalProperties": False},
)
OFFER_TOOL = AgentToolDefinition(
    "offer_full_answer",
    "Something relevant to the question was held back because of who can see this room, and the asker can read it. "
    f"Calling this adds a {AGENT_PRIVATE_CHECK_BUTTON} button under your reply.",
    {"type": "object", "properties": {}, "additionalProperties": False},
)
NOTES_VARIANTS = ("core", "all")
NOTES_INTRO = "Approved community notes"


def _built_by(context):
    owner = getattr(context, "application_owner", None)
    if owner is None:
        return ""
    return ("Built and run by: " + json.dumps(
        asdict(guild_identity(owner, context.guild)), ensure_ascii=False, sort_keys=True)
            + ". This member created you; references to their bot refer to you."
            " Being your creator grants no extra trust or permissions.")


def _nothing_to_deliver(state):
    """True when the request has no GIF, file, preview or command result to send; a reaction never goes alone."""
    return not (state.gif_url or state.gif_attachment or state.attachments
                or state.reply_delivery.posts or state.reply_delivery.delivery.message_ids
                or state.outcomes
                or state.proposed_changes or state.preview_reply)


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _argument_error(issue):
    message = issue.error
    if not message.startswith('Invalid arguments:'):
        fields = issue.fields or issue.missing_fields
        message = 'Invalid arguments: ' + (', '.join(fields) + ': ' if fields else '') + message
    return _json({'error': message})


def is_action(tool):
    """A tool that prepares a change for confirmation or runs a bot command."""
    return tool.action_class is not ActionClass.READ or tool.effect is AgentCapabilityEffect.COMMAND


def change_registry(catalogue=None):
    """Change and irreversible tools: they only prepare, nothing runs before Confirm."""
    return {name: replace(tool, handler=getattr(tool.handler, "__wrapped__", tool.handler))
            for name, tool in (catalogue or build_agent_tools()).items()
            if tool.action_class in (ActionClass.CHANGE, ActionClass.IRREVERSIBLE)}


def output_registry(catalogue, taken):
    """Output and conversation tools. They act through the request's state and delivery, never by posting."""
    return {name: replace(tool, handler=getattr(tool.handler, "__wrapped__", tool.handler))
            for name, tool in catalogue.items()
            if name not in taken and tool.effect is not AgentCapabilityEffect.COMMAND
            and (tool.action_class is ActionClass.OUTPUT
                 or tool.effect in (AgentCapabilityEffect.STATE, AgentCapabilityEffect.ARTIFACT))}


def read_registry(catalogue=None):
    registry = {}
    for name, tool in (catalogue or build_agent_tools()).items():
        if tool.effect is not AgentCapabilityEffect.READ or tool.action_class is not ActionClass.READ:
            continue
        definition = tool.definition
        if {'cursor', 'report_id', 'offset', 'limit'} <= definition.parameters.get('properties', {}).keys():
            properties = {key: value for key, value in definition.parameters['properties'].items()
                          if key != 'cursor'}
            for field in ('offset', 'limit'):
                properties[field] = {**properties[field], 'description': 'A row window over the saved result.'}
            fields = [field for field in SLICE_COLUMNS if field in properties]
            narrowing = (fields[0] if len(fields) == 1 else ', '.join(fields[:-1]) + ' or ' + fields[-1]) if fields else ''
            properties['report_id'] = {**properties['report_id'], 'description':
                'Reuse a saved result: ' + (f'choose some of the {narrowing} it was read with, or ' if narrowing else '')
                + 'export it; any other change reads again.'}
            definition = replace(definition, parameters={**definition.parameters, 'properties': properties})
        registry[name] = replace(tool, definition=definition, handler=tool.handler.__wrapped__)
    return registry


def request_registry(registry, context):
    """The tools this request may use. A private pass keeps reads and artifact tools only."""
    if not context.private_pass:
        return registry
    return {name: tool for name, tool in registry.items()
            if tool.action_class is ActionClass.READ or tool.effect is AgentCapabilityEffect.ARTIFACT}


def _role_names(member):
    names = [role.name for role in reversed(member.roles) if not role.is_default()]
    return ", ".join(names) if names else "none"


def _permission_line(context):
    can, cannot = asker_permissions(context.member, context.source_message.channel)
    names = lambda keys: "; ".join(PERMISSIONS[key] for key in keys) or "none"
    return ("Requester Discord permissions here (direct Discord actions only: posting, pinging, pins, "
            "reactions, files, threads; bot features follow their own role rules): "
            f"can: {names(can)}. cannot: {names(cannot)}.")


def _request_context(context):
    mentioned = [{"member_id": member.id, "display_name": member.display_name}
                 for member in getattr(context.source_message, "mentions", ())]
    instructions = [asdict(instruction) for instruction in context.state.authorized_instructions]
    return (f"Request context\nElbow Helper Discord ID (you): {context.guild.me.id}\n"
            f"Requester Discord ID: {context.member.id}\n"
            f"Requester display name: {context.member.display_name}\n"
            f"Requester Discord roles: {_role_names(context.member)}\n"
            f"{_permission_line(context)}\n"
            f"Channel Discord ID: {context.source_message.channel.id}\n"
            f"Request time: {context.source_message.created_at.isoformat()}"
            + (f"\nMentioned members: {_json(mentioned)}" if mentioned else "")
            + (f"\nInstructions remembered for this thread: {_json(instructions)}" if instructions else ""))


def _select_notes(store, levels, variant="core"):
    """The notes the prompt carries in full, and an index of every note the asker may open."""
    if variant not in NOTES_VARIANTS:
        raise ValueError(f"notes must be one of {NOTES_VARIANTS}")
    sections = store.load().visible_sections(levels)
    if variant == "all":
        return list(sections), ""
    return ([section for section in sections if section.file_name.startswith(("core", "clan-profiles", "culture"))],
            _note_index(sections))


def _note_index(sections):
    files = {}
    for section in sections:
        files.setdefault(section.file_name, {})[section.title] = None
    if not files:
        return ""
    lines = ["Every note section you may open, by file. The notes above are part of these; open the rest "
             "with search_approved_knowledge by title."]
    for name, titles in files.items():
        lines.append(f"{name}:")
        lines.extend(f"- {title}" for title in titles)
    return "\n".join(lines)


class LoopService:
    """Answers one request in one model conversation. Nothing per request is kept on the service."""

    def __init__(self, model, stored_data, notes="core"):
        self.model = model
        self.stored_data = stored_data
        self.notes = notes
        catalogue = build_agent_tools()
        self.registry = read_registry(catalogue)
        self.registry["query_bot_data"] = replace(self.registry["query_bot_data"], handler=stored_data.handler)
        self.changes = change_registry(catalogue)
        self.outputs = output_registry(catalogue, {*self.registry, *self.changes})
        # Command tools may not take the name of any other tool.
        self.reserved_names = {*catalogue, SOCIAL_REPLY_NAME}
        # The latest request's record, for diagnostics; concurrent requests overwrite it.
        self.last_run = None
        self.member_usage = MemberUsageLimiter()

    def request_tools(self, context):
        """Return the tools this request may use."""
        registry = {**self.registry, **self.outputs}
        if not context.private_pass and getattr(context.bot, "tree", None) is not None:
            registry.update(self.changes)
            command_tools, capabilities = build_command_tools(
                context.bot, enabled_adapters(), reserved_names=self.reserved_names)
            member = context.guild.get_member(context.member.id)
            held = {role.id for role in member.roles} if member else set()
            registry.update({
                name: tool for name, tool in command_tools.items()
                if capabilities[name].visible_to is None or held.intersection(capabilities[name].visible_to)})
            if not role_change_access(context)["requester_allowed"]:
                for name in ("add_discord_roles", "remove_discord_roles"):
                    registry.pop(name, None)
        return request_registry(registry, context)

    async def answer(self, *, question, context, standing_scope=""):
        state = context.state
        state.research_answer_completed = False
        state.feedback_lookup_problem = False
        await request_scope(context, refresh=True)
        await require_evidence_access(context)
        if context.history:
            await prepare_history(context, context.history, state.authorized_checkpoint)
        state.request_text = question
        state.resolved_members.clear()
        # The asker is always a resolved member: "give me the role" needs no mention of themselves.
        state.member_self_target = True
        if not context.private_pass:
            async def run_private():
                return await private_answer_pass(self, context, question, standing_scope)
            state.private_answer_run = run_private
        record = OperatorDebugRecord(question)
        context = replace(context, operator_debug=record)
        run = self.last_run = LoopRun(self)
        admitted = None
        try:
            if not leadership_asker(context):
                admitted = self.member_usage.admit(context.guild.id, context.member.id)
            answer = await run.answer(question, context, standing_scope)
            record.status = "incomplete" if record.error else "completed"
            record.answer = answer
            state.research_answer_completed = not record.error and not state.terminal_cause
            return answer
        except MemberUsageLimit as error:
            state.member_limit_refused = True
            state.scope_audit.offered = False
            record.status = "member_limit"
            record.answer = error.reply
            return error.reply
        except asyncio.CancelledError:
            record.status = "cancelled"
            raise
        except AgentAccessLost as error:
            record.error = str(error)
            record.status = "access_lost"
            raise
        except Exception as error:
            record.error = str(error)
            if record.status == "incomplete":
                record.status = "failed"
            raise
        finally:
            if admitted is not None:
                # Banter is a request that ended with the banter reply; anything else counts as a question.
                self.member_usage.settle(context.guild.id, context.member.id, admitted,
                                         banter=run.banter.reply is not None)
            record.usage = run.usage_summary()
            LOGGER.info("Agent usage: request=%s status=%s invoker=%s rounds=%s tools=%s "
                        "prompt_tokens=%s completion_tokens=%s cache_hit_tokens=%s cache_miss_tokens=%s "
                        "unknown_token_rounds=%s", getattr(context.source_message, "id", None), record.status,
                        context.member.id, record.usage["rounds"], len(run.calls), record.usage["prompt_tokens"],
                        record.usage["completion_tokens"], record.usage["cache_hit_tokens"],
                        record.usage["cache_miss_tokens"], record.usage["unknown_token_rounds"])
            record.terminal_cause = state.terminal_cause
            log_scope(context)
            if context.transcript_archive is not None:
                try:
                    context.transcript_archive.enqueue_debug(context, record)
                except Exception:
                    LOGGER.exception("Agent operator debug archive enqueue failed")


class LoopRun:
    """What one request did: its lookups, pages and the notes it was given."""

    def __init__(self, service):
        self.service = service
        self.calls = []
        self.rounds = []
        self.knowledge = []
        self.pages = {}
        self.page_base = 0
        self.system = {}
        self.note_sections = 0
        self.channels = ""
        self.banter = Banter()
        self.banter_context = ""
        self.registry = {}
        self.usage = RequestUsage()

    def usage_summary(self):
        """What the operator record and the 👎 card show: rounds and tokens."""
        usage = self.usage
        return {**asdict(usage), "rounds": usage.attempted_rounds,
                "unknown_token_rounds": usage.unknown_token_rounds,
                "unknown_cache_rounds": usage.unknown_cache_rounds}

    async def _round(self, session, context, results):
        """One model round. A round cut at the output limit is continued once; None when that is cut too."""
        step = await self._advance(session, context, results)
        if not step.output_limit_reached:
            return step
        LOGGER.warning("Agent model output limit reached: request=%s round=%s",
                       getattr(context.source_message, "id", None), self.usage.attempted_rounds)
        if step.tool_calls:
            # A cut call never runs: each is answered with a request to submit it whole.
            refusals = tuple(AgentToolResult(call.call_id, _json({"error": REPEAT_TOOL_CALL_INSTRUCTION}))
                             for call in step.tool_calls)
            continuation = await self._advance(session, context, refusals)
        else:
            continuation = await self._advance(session, context, (), CONTINUE_ANSWER_INSTRUCTION)
            if step.content and not continuation.tool_calls:
                continuation = replace(continuation, content=step.content + "\n" + continuation.content)
        return None if continuation.output_limit_reached else continuation

    async def _advance(self, session, context, results, continuation_instruction=None):
        """One model round. A provider failure is the request failing, not an answer."""
        record = context.operator_debug
        self.usage.attempted_rounds += 1
        started = time.monotonic()
        step = None
        outcome = "failed"
        try:
            step = await session.advance(
                results, reasoning_effort=AgentReasoningEffort.HIGH,
                **({"continuation_instruction": continuation_instruction} if continuation_instruction else {}))
            self.usage.observe(step.usage)
            outcome = "completed"
            return step
        except TextGenerationError as error:
            record.status = "provider_error"
            raise AgentUnavailableError(str(error)) from error
        finally:
            record.rounds.append({
                "round": self.usage.attempted_rounds, "effort": AgentReasoningEffort.HIGH.value, "outcome": outcome,
                "elapsed_ms": int((time.monotonic() - started) * 1000),
                "usage": asdict(step.usage) if step is not None else None,
                "content": step.content if step is not None else None,
            })

    async def _system_prompt(self, context):
        service = self.service
        state = context.state
        state.knowledge_channel_access = await channel_knowledge_access(context)
        notes, note_index = await asyncio.to_thread(_select_notes, context.knowledge_store,
                                                    state.knowledge_channel_access, service.notes)
        self.note_sections = len(notes)
        meanings = await asyncio.to_thread(load_meanings, context.bot.paths.data_root,
                                           state.knowledge_channel_access)
        structure = Structure(Server.from_guild(context.guild))
        self.channels = structure.channel_text(context.member.id, context.guild.me.id)
        self.knowledge.extend({"title": note.title, "section_id": _section_id(note),
                               "visibility": note.visibility, "source": "system_core"} for note in notes)
        state.knowledge_core_available = bool(notes)
        state.required_access.update(note.visibility for note in notes if note.visibility != "public")
        self.knowledge.extend({"title": note.title, "section_id": _section_id(note),
                               "visibility": note.visibility, "source": "system_map"} for note in meanings)
        state.required_access.update(note.visibility for note in meanings if note.visibility != "public")
        parts = [SYSTEM_OPENING]
        if not context.private_pass:
            # Only a request that offers the banter reply tool carries the banter rules.
            parts.append(BANTER_SYSTEM)
        parts.append(NOTES_INTRO)
        parts.extend(f"### {note.title} ({note.file_name})\n{note.body}" for note in notes)
        if note_index:
            parts.append(note_index)
        parts.extend(("Read-only SQLite catalogue", service.stored_data.describe(context)))
        parts.extend(("Community map: what roles and terms mean",
                      "\n\n".join(f"## {note.title} [{note.visibility}]\n{note.body}" for note in meanings),
                      "Community map: roles and channels on the server",
                      structure.fixed_text(structure.members[context.member.id].roles)))
        # The emoji meanings submit_social_reply tells the model to use; the same for every request on a server.
        if not context.private_pass and (reactions := server_reaction_context(context.guild)):
            parts.append(reactions)
        system = "\n\n".join(parts)
        encoded = system.encode("utf-8")
        self.system = {"system_text_sha256": sha256(encoded).hexdigest(), "system_text_bytes": len(encoded)}
        return system

    def _first_message(self, context, question, standing_scope=""):
        combo = COMBO_AVAILABLE if context.state.reaction_combo_due else ""
        built_by = _built_by(context)
        return (f"{_request_context(context)}\n" + (f"{built_by}\n" if built_by else "")
                + (f"{combo}\n" if combo else "")
                + (f"{self.banter_context}\n" if self.banter_context else "")
                + f"\nCommunity map: roles and channels on the server\n"
                f"{self.channels}\n\n" + (f"{standing_scope}\n\n" if standing_scope else "") + question)

    async def answer(self, question, context, standing_scope=""):
        referenced = next((item for item in context.attachment_sources
                           if item.id != context.source_message.id), None)
        context, prior = await prior_messages(context, referenced, resolve_reference)
        # Replayed outputs keep their result ids, so new ids start after them.
        self.page_base = last_result_id(prior)
        # A remembered instruction quotes a message from its channel.
        context.state.source_channels.update(
            instruction.source_channel_id for instruction in context.state.authorized_instructions)
        system = await self._system_prompt(context)
        images = await request_images(replace(context, history=context.state.authorized_history or ()))
        registry = self.service.request_tools(context)
        # Every second reply under the channel's pacing may carry a reaction combination.
        context.state.reaction_combo_due = "react_to_request" in registry and reply_combo_due(context)
        LOGGER.info("Agent reaction combo: request=%s offered=%s gate=%s history=%s revision=%s channel_revision=%s",
                    getattr(context.source_message, "id", None), context.state.reaction_combo_due,
                    combo_slot_status(context), context.state.channel_bot_combos,
                    context.state.reaction_combo_revision, context.state.channel_reactions.combo_revision)
        # The offer stays listed all request (it refuses when nothing was held back) so the tool list,
        # and with it the prompt cache, does not change mid-request. A private pass can never offer.
        # Memes go out through submit_social_reply, so find_gif is not listed; the reply tool runs it. A private
        # pass sends nothing, so it has no reply tool.
        self.registry = registry
        listed = {name: tool for name, tool in registry.items() if name != GIF_NAME}
        banter = () if context.private_pass else (
            banter_definition(registry, combo_due=context.state.reaction_combo_due),)
        if banter:
            recent_memes = await asyncio.to_thread(recent_meme_context, context)
            self.banter_context = "\n".join(filter(None, (recent_reaction_context(context), recent_memes)))
        tools = [*model_tools(listed), PAGE_TOOL, *(() if context.private_pass else (OFFER_TOOL,)), *banter]
        # Offering a reaction combination changes only the end of the reply tool's schema, so it is not in the key.
        keyed = [*tools[:-1], banter_definition(registry)] if banter else tools
        fixed = _json([system, sorted([tool.name, tool.description, tool.parameters] for tool in keyed)])
        session = self.service.model.create_agent_session(
            system_prompt=system, prompt=self._first_message(context, question, standing_scope), tools=tools,
            images=images, prior_messages=prior, sent_reasoning_effort="high",
            max_output_tokens=limits.AGENT_MAX_OUTPUT_TOKENS,
            prompt_cache_key=sha256(fixed.encode("utf-8")).hexdigest(),
        )
        if session is None:
            raise AgentUnavailableError("The AI backend is not configured")
        results = []
        while True:
            await require_evidence_access(context)
            step = await self._round(session, context, results)
            if step is None:
                context.operator_debug.error = AGENT_ANSWER_UNFINISHED
                await require_evidence_access(context)
                return await self._deliverable(context, AGENT_ANSWER_UNFINISHED)
            lookups = []
            self.rounds.append({"lookups": lookups})
            if not step.tool_calls:
                await require_evidence_access(context)
                if not step.content.strip() and _nothing_to_deliver(context.state):
                    raise AgentUnavailableError("The model returned no answer")
                return await self._deliverable(context, step.content)
            results = []
            for call in step.tool_calls:
                output = await self._run_call(context, listed, call, lookups)
                results.append(AgentToolResult(call.call_id, output))
            if self.banter.reply is not None:
                # The reply tool's line, or the meme it sent, is the whole answer.
                await require_evidence_access(context)
                return await self._deliverable(context, self.banter.reply)
            if step.content.strip() and all(
                lookup["status"] == "completed"
                and (tool := listed.get(lookup["capability"])) is not None
                and tool.finishes_reply_with_text
                for lookup in lookups
            ):
                await require_evidence_access(context)
                return await self._deliverable(context, step.content)

    async def _deliverable(self, context, text):
        """Add command results to the reply and track any separate Confirm card."""
        state = context.state
        response = None
        if state.proposed_changes:
            await prepare_preview(context)
            response = preview_text(state.proposed_changes)
        if state.outcomes and not state.reply_delivery.posts:
            text = "\n\n".join(filter(None, (text, command_reply(state.outcomes, has_findings=bool(text.strip())))))
        if response is None:
            return text
        if not text.strip():
            return ""
        # The model's text goes out with the preview as its own message, after the text.
        state.preview_reply = response
        state.preview_first = False
        return text

    async def _run_call(self, context, registry, call, lookups):
        started = time.monotonic()
        entry = {"capability": call.name, "arguments": call.arguments, "call_id": call.call_id,
                 "source": "page" if call.name == PAGE_TOOL.name else "live"}
        self.calls.append(entry)
        lookups.append(entry)
        context.operator_debug.tool_calls.append(entry)
        try:
            try:
                arguments = json.loads(call.arguments)
            except (TypeError, ValueError):
                arguments = None
            if isinstance(arguments, dict):
                entry["arguments"] = arguments
            raw = await self._execute(context, registry, call.name, arguments, call)
            result = json.loads(raw)
            error = result.get("error")
            if error is None and result.get("flags", {}).get("status") == "failed":
                error = result.get("cause", "failed")
            tool = registry.get(call.name)
            size = len(raw.encode("utf-8"))
            entry.update(result=result, result_bytes=size, error=error, status="error" if error else "completed")
            if call.name == "search_approved_knowledge":
                self.knowledge.extend({key: section.get(key) for key in
                                       ("title", "section_id", "visibility", "available_privately",
                                        "section_offset", "next_section_offset")}
                                      for section in (*result.get("sections", []), *result.get("hits", [])))
            if call.name in (OFFER_TOOL.name, SOCIAL_REPLY_NAME) or (tool is not None and is_action(tool)):
                # Offers, replies, changes and commands are not lookups: their outcomes reach the thread through the turn.
                entry["sent_bytes"] = size
                if call.name not in (OFFER_TOOL.name, SOCIAL_REPLY_NAME):
                    entry["source"] = "action"
                return raw
            if call.name != PAGE_TOOL.name:
                shown = shape(context, arguments, result, name=call.name)
                result_id = str(self.page_base + len(self.calls))
                pages = row_pages(shown, PAGE_BYTES, result_id=result_id)
                entry["shown_bytes"] = len(_json(shown).encode("utf-8"))
                if len(pages) > 1:
                    self.pages[result_id] = pages
                    entry.update(result_id=result_id, pages=len(pages))
                    raw = self._page(result_id, 1)
                else:
                    raw = _json(shown)
            entry["sent_bytes"] = len(raw.encode("utf-8"))
            retained = {"capability": call.name, "arguments": arguments, "output": raw,
                        "source_channels": sorted(context.state.source_channels),
                        "required_access": sorted(context.state.required_access),
                        "knowledge_sources": sorted(context.state.knowledge_sources)}
            context.state.lookup_results.append(_json(retained))
            entry.update({key: retained[key] for key in
                          ("source_channels", "required_access", "knowledge_sources")})
            entry["output"] = raw
            return raw
        except BaseException as error:
            entry.update(status="failed", error_type=type(error).__name__, error=str(error), result_bytes=0)
            raise
        finally:
            entry["elapsed_ms"] = int((time.monotonic() - started) * 1000)

    async def _execute(self, context, registry, name, arguments, call=None):
        await require_evidence_access(context)
        if name == SOCIAL_REPLY_NAME:
            if context.private_pass or call is None:
                return _json({"error": "unavailable_lookup"})
            # The reply tool sends through the full registry: find_gif is run by it, never listed to the model.
            return await self.banter.call(self._execute, context, self.registry, call, arguments)
        if name == PAGE_TOOL.name:
            issue = argument_issue({name: RegisteredAgentTool(PAGE_TOOL, self._page)}, name, arguments)
            if issue is not None:
                return _argument_error(issue)
            return self._page(arguments["result_id"], arguments["page"])
        if name == OFFER_TOOL.name:
            if not private_check_available(context):
                return _json({"error": "unavailable_lookup"})
            context.state.scope_audit.offered = True
            return _json({"offered": True})
        tool = registry.get(name)
        if tool is None:
            return _json({"error": "unavailable_lookup"})
        acting = is_action(tool)
        step_id = f"s{len(self.calls)}"
        # A change may point at an earlier change's result, such as a thread it creates.
        earlier = {action.step_id for action in context.state.proposed_changes if action.step_id}
        issue = argument_issue(registry, name, arguments, earlier if acting else None, step_id=step_id)
        if issue is not None:
            return _argument_error(issue)
        selected = original_tool(registry, name, arguments)
        if issue := await disclosure_issue(context, registry, {"capability": name, "arguments": arguments}):
            return _json(issue)
        try:
            scope = compile_capability_call(
                tool, arguments, context.state.report_sources, contract=selected.contract if selected else None,
            )
        except CapabilityBindError as error:
            return _json({"error": str(error), "cause": "invalid_arguments"})
        if not acting:
            return await execute_tool(name=name, handler=tool.handler, arguments=arguments,
                                      capability_scope=scope, context=context)
        return await self._act(context, tool, name, arguments, scope, step_id)

    async def _act(self, context, tool, name, arguments, scope, step_id):
        """Prepare a change or run a command. A bundle the confirmation could not show is taken back."""
        state = context.state
        before = len(state.proposed_changes)
        snapshot = tool_state_snapshot(context)
        local = replace(context, member=require_access(context.guild, context.member.id,
                                                       context.source_message.channel))
        raw = await execute_tool(
            name=name, handler=tool.handler, arguments=arguments, capability_scope=scope, context=local,
            action_class=tool.action_class,
            timeout_seconds=(limits.COMMAND_TIMEOUT_SECONDS if tool.effect is AgentCapabilityEffect.COMMAND
                             else limits.TOOL_TIMEOUT_SECONDS))
        result = json.loads(raw)
        if tool.effect is AgentCapabilityEffect.ARTIFACT and "error" not in result:
            # An artifact may carry text from knowledge titles the asker was shown.
            state.required_access.update(state.knowledge_title_access)
        prepared = range(before, len(state.proposed_changes))
        for index in prepared:
            proposal = state.proposed_changes[index]
            if not isinstance(proposal, PreparedAction):
                continue
            if proposal.action_class is not tool.action_class:
                LOGGER.error("Agent action class mismatch: capability=%s step=%s registered=%s prepared=%s",
                             name, step_id, tool.action_class, proposal.action_class)
                restore_tool_state(context, snapshot)
                return _json({"error": ACTION_UNAVAILABLE})
            state.proposed_changes[index] = replace(
                proposal, step_id=step_id, capability_name=name, checked_arguments=deepcopy(arguments))
        if not prepared:
            if (tool.action_class in (ActionClass.CHANGE, ActionClass.IRREVERSIBLE)
                    and result.get("cause") in ("access_denied", "scope_refused") and "error" in result):
                return _json({**result, "note": REFUSED_CHANGE_NOTE})
            return raw
        try:
            check_bundle(tuple(state.proposed_changes))
        except ActionRefused as error:
            refusal = error.result()
        except ValueError as error:
            refusal = {"error": str(error)}
        else:
            return _json({**result, "step_id": step_id})
        del state.proposed_changes[before:]
        return _json({**refusal, "note": REFUSED_CHANGE_NOTE})

    def _page(self, result_id, number):
        pages = self.pages.get(result_id, ())
        if not 1 <= number <= len(pages):
            return _json({"error": "unavailable_result_page"})
        info, content = pages[number - 1]
        return _json({"result_page": {"result_id": result_id, **info}, **content})
