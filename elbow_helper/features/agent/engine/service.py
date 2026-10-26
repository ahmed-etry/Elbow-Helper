"""Bounded orchestration for the agent."""

from __future__ import annotations

import logging
import json
from dataclasses import replace

import asyncio
from datetime import datetime, timezone
import time
from elbow_helper.infrastructure.ai import AgentModel
from elbow_helper.infrastructure.ai import TextGenerationError
from elbow_helper.infrastructure.ai.agent import AgentReasoningEffort
from ..models import AgentRequestContext
from ..knowledge.tools import channel_knowledge_access
from ..research.directory import scoped_channel_directory
from ..knowledge.answers import CHANNEL_FIRST_INSTRUCTION, private_answer_pass, scoped_answer
from ..conversation.debug import OperatorDebugRecord
from ..actions.contracts import ActionClass
from ..discord_actions.roles import role_change_access
from ..access import (
    AgentAccessLost,
    require_evidence_access,
)
from ..conversation.context import (
    INPUT_TARGET_TOKENS, SOCIAL_INPUT_TARGET_TOKENS, CompiledContext, compile_context, estimate_tokens,
)
from ..conversation.reply_context import prepare_reply_context, REPLY_CONTEXT_INSTRUCTION
from ..discord_actions.reactions import recent_reaction_context, server_reaction_context
from ..discord_actions.reaction_pacing import reply_combo_due, combo_slot_context, combo_slot_status
from ..discord_actions.meme_usage import recent_meme_context
from ..disclosure import can_disclose_provenance, request_scope, log_scope
from .registry import build_agent_tools
from .self_knowledge import capability_context
from ..families.catalogue import discovery_tool, FAMILY_INSTRUCTION
from .usage import RequestUsage
from .budgets import ContextBudget
from .routing import (
    EXPAND_DEFINITION,
    judge_route, RouteDecision, SOCIAL_CAPABILITIES, SOCIAL_INSTRUCTION, ROUTE_OUTPUT_TOKENS,
)
from .social_reply import social_reply_definition, SOCIAL_REPLY_INSTRUCTION
from ..reports.tools import filter_fields
from ..plan.format import plan_definition, system_instructions
from ..plan.planning import PlanNotSettled
from ..capabilities import enabled_adapters
from ..commands.bridge import build_command_tools
from ..prompts import RESEARCH_RULES, VOICE_RULES, GAME_ADVICE_RULES
from ..actions.preview import is_preview_reply, preview_text
from ..research.member_provenance import member_target_issue
from . import budgets as limits
from .rounds import AgentGracefulEnd, AgentLimitReached, ModelRounds
from .steps import PlanRunner
from .flow import AnswerFlow
from .unfinished import unfinished_answer
from .tool_call import execute_tool

LOGGER = logging.getLogger(__name__)


class AgentUnavailableError(RuntimeError):
    """Raised when an agent request cannot produce a final answer."""


class AgentService:
    """Plan checked reads and answer from their results."""

    def __init__(self, model: AgentModel, *, data_guide=None) -> None:
        self._model = model
        self._data_guide = data_guide

    execute_tool = staticmethod(execute_tool)


    @staticmethod
    def _log_completion(
        *,
        context: AgentRequestContext,
        rounds: int,
        tool_calls: int,
        evidence_characters: int,
        usage: RequestUsage,
        status: str,
        elapsed_ms: int,
    ) -> None:
        LOGGER.info(
            (
                (
                    'Agent usage: request=%s status=%s '
                    'elapsed_ms=%s invoker=%s channel=%s '
                    'rounds=%s tools=%s evidence_chars=%s '
                    'prompt_tokens=%s completion_tokens=%s '
                    'cache_hit_tokens=%s cache_miss_tokens=%s '
                    'attempted_rounds=%s unknown_token_rounds=%s '
                    'unknown_cache_rounds=%s'
                )
            ),
            getattr(context.source_message, "id", None),
            status,
            elapsed_ms,
            context.member.id,
            context.source_message.channel.id,
            rounds,
            tool_calls,
            evidence_characters,
            usage.prompt_tokens,
            usage.completion_tokens,
            usage.cache_hit_tokens,
            usage.cache_miss_tokens,
            usage.attempted_rounds,
            usage.unknown_token_rounds,
            usage.unknown_cache_rounds,
        )

    def _start_flow(
        self,
        *,
        question: str,
        local_context: str,
        context: AgentRequestContext,
        conversation_history: str,
        images=(),
        route: RouteDecision | None = None,
        research_history=None,
        research_checkpoint=None,
    ):
        context.state.grounding_evidence.clear()
        if context.state.local_messages:
            context.state.grounding_evidence["local_context"] = {"messages": context.state.local_messages}
        game_advice = route is not None and route.game_advice
        registry = {} if game_advice else build_agent_tools()
        capability_registry = dict(registry)
        gif_tool = registry.pop("find_gif", None)
        social = route is not None and route.social
        if social and gif_tool is not None:
            registry["find_gif"] = gif_tool
        family_catalogue = any(not tool.default_visible for tool in registry.values())
        command_capabilities = {}
        actions_available = not context.private_pass and getattr(context.bot, "tree", None) is not None
        if not actions_available:
            registry = {
                name: tool
                for name, tool in registry.items()
                if tool.action_class not in (ActionClass.CHANGE, ActionClass.IRREVERSIBLE)
            }
        if actions_available and not game_advice:
            command_tools, command_capabilities = build_command_tools(
                context.bot,
                enabled_adapters(),
            )
            capability_registry.update(command_tools)
            current_member = context.guild.get_member(context.member.id)
            held_roles = {role.id for role in current_member.roles} if current_member else set()
            command_tools = {name: tool for name, tool in command_tools.items()
                if name not in command_capabilities or command_capabilities[name].visible_to is None
                or held_roles.intersection(command_capabilities[name].visible_to)}
            registry.update({name: replace(tool, default_visible=not family_catalogue) for name, tool in command_tools.items()})
        if context.private_pass:
            registry = {name: tool for name, tool in registry.items()
                        if tool.action_class is ActionClass.READ
                        or tool.effect.value == "artifact"}
        if not role_change_access(context)['requester_allowed']:
            for name in ('add_discord_roles', 'remove_discord_roles'):
                registry.pop(name, None)
        specialist_registry = dict(registry)
        full_registry = {name: tool for name, tool in registry.items()
                         if tool.default_visible and name != "find_gif"}
        if family_catalogue:
            full_registry["open_capability_group"] = discovery_tool(
                specialist_registry, registry, lambda: (session.replace_tools((plan_definition(registry),)),
                                                      setattr(budget, "request_limits", limits.RESEARCH_LIMITS)))
        capability_registry.update(full_registry)
        if not social:
            registry.clear()
            registry.update(full_registry)
        if social:
            selected_social = {name: tool for name, tool in registry.items() if name in SOCIAL_CAPABILITIES}
            registry.clear()
            registry.update(selected_social)

        research_context = ""
        def research_instructions():
            nonlocal research_context
            levels = context.state.scope.levels if context.state.scope else frozenset()
            knowledge = context.knowledge_store.load().prompt(
                context.state.knowledge_channel_access,
            ) if context.knowledge_store else None
            if knowledge:
                context.state.required_access.update(knowledge.required_access)
            # Only immutable instructions and signatures belong in the cached
            # system prefix. Access-scoped indexes and locators follow it.
            research_context = ("<community_knowledge>\n" + knowledge.index
                                + "\n</community_knowledge>") if knowledge else ""
            if self._data_guide and not family_catalogue:
                research_context += "\n<data_guide>\n" + self._data_guide.for_levels(levels) + "\n</data_guide>"
            if context.state.channel_directory:
                research_context += (
                    "\n\n<readable_channel_directory>\n"
                    "Live channel IDs and names available in this destination scope. This is a "
                    "locator, not message evidence. Static knowledge describes room purposes, "
                    "not a refusal. Read relevant discussion rooms here before concluding no "
                    "discussion is visible. This list omits private and archived threads; use "
                    "scoped discovery for those. Each read still checks current access.\n"
                    + context.state.channel_directory + "\n</readable_channel_directory>"
                )
            return (system_instructions(full_registry, actions_enabled=actions_available) + "\n\n" + RESEARCH_RULES
                    + ("\n\n" + FAMILY_INSTRUCTION if family_catalogue else ""))

        definition = plan_definition(registry)
        context.state.reaction_combo_due = social and "react_to_request" in registry and reply_combo_due(context)
        LOGGER.info("Agent reaction combo: request=%s offered=%s gate=%s history=%s revision=%s channel_revision=%s",
                    getattr(context.source_message, "id", None), context.state.reaction_combo_due,
                    combo_slot_status(context), context.state.channel_bot_combos,
                    context.state.reaction_combo_revision, context.state.channel_reactions.combo_revision)
        definitions = () if game_advice else (
            (social_reply_definition(registry, combo_due=context.state.reaction_combo_due), EXPAND_DEFINITION)
            if social else (definition,)
        )
        LOGGER.info("Agent social outputs: request=%s direct_tools=%s",
                    getattr(context.source_message, "id", None),
                    [tool.name for tool in definitions])
        system_prompt = VOICE_RULES + '\n\n' + GAME_ADVICE_RULES if game_advice else (
            system_instructions(registry, actions_enabled=actions_available, social=True)
            + "\n\n" + SOCIAL_INSTRUCTION + "\n\n" + SOCIAL_REPLY_INSTRUCTION
            if social else research_instructions()
        )
        system_prompt += "\n\n" + CHANNEL_FIRST_INSTRUCTION + "\n\n" + REPLY_CONTEXT_INSTRUCTION
        context.state.request_text = question
        context.state.family_catalogue = family_catalogue
        compiled = compile_context(
            question=question,
            local_context="\n".join(filter(None, (
                research_context, local_context,
                server_reaction_context(context.guild) if social else '',
                recent_reaction_context(context) if social else '',
                combo_slot_context(context) if social else '',
                recent_meme_context(context) if social else '',
            ))),
            context=context,
            tools=definitions,
            conversation_history=conversation_history,
            report_filter_fields=filter_fields(registry),
            system_prompt=system_prompt,
            answer_local_context=local_context,
            target_tokens=SOCIAL_INPUT_TARGET_TOKENS if social else INPUT_TARGET_TOKENS,
            social=social,
        ) if not game_advice else self._game_advice_context(question, local_context, conversation_history, context, system_prompt)
        request_prompt = (
            compiled.prompt.replace(
                "<images>\nNo images.\n</images>",
                "<images>\n" + ("\n".join(
                    [image.label for image in images] + context.state.image_source_status
                ) or "No images.")
                + "\n</images>",
            ) + "\nCurrent UTC: " + datetime.now(timezone.utc).isoformat()
        )
        session = self._model.create_agent_session(
            system_prompt=system_prompt,
            prompt=request_prompt,
            tools=definitions,
            max_output_tokens=limits.AGENT_MAX_OUTPUT_TOKENS,
            **({"images": images} if images else {}),
        )
        if session is None:
            raise AgentUnavailableError("The AI backend is not configured")
        budget = ContextBudget(
            context_window_tokens=getattr(session, "context_window_tokens", None),
            output_reserve=limits.INITIAL_MAX_OUTPUT_TOKENS,
            next_input_estimate=compiled.estimated_input_tokens
            + estimate_tokens(request_prompt)
            - estimate_tokens(compiled.prompt),
            final_answer_reserve=limits.FINAL_MAX_OUTPUT_TOKENS,
            request_limits=(limits.SOCIAL_LIMITS if social and not game_advice
                            else limits.LOOKUP_LIMITS if social or game_advice else limits.FAMILY_LIMITS
                            if family_catalogue else limits.RESEARCH_LIMITS),
        )
        usage = RequestUsage()
        if route is not None:
            usage.attempted_rounds += 1
            usage.observe(route.usage)
            budget.spent_rounds += 1
            budget.spent_input += (route.usage.prompt_tokens if route.usage.prompt_tokens is not None
                                   else route.estimated_input)
            budget.spent_output += (route.usage.completion_tokens if route.usage.completion_tokens is not None
                                    else ROUTE_OUTPUT_TOKENS)
        request_id = getattr(context.source_message, "id", None)
        rounder = ModelRounds(
            context=context,
            budget=budget,
            usage=usage,
            session=session,
            request_id=request_id,
        )
        if capability_registry:
            rounder.capability_context = lambda: capability_context(
                capability_registry, context, command_capabilities,
            )
        rounder.social_route = social
        rounder.game_advice_route = game_advice
        rounder.request_effort = AgentReasoningEffort.LOW if social or game_advice else AgentReasoningEffort.HIGH
        if social:
            async def expand_context():
                if not context.state.channel_directory:
                    context.state.channel_directory = await scoped_channel_directory(context)
                instructions = research_instructions()
                context.state.authorized_history = research_history
                context.state.authorized_checkpoint = research_checkpoint
                expanded_context = compile_context(
                    question=question, local_context="\n".join(filter(None, (research_context, local_context))), context=context,
                    tools=(plan_definition(full_registry),),
                    conversation_history=conversation_history,
                    report_filter_fields=filter_fields(full_registry), system_prompt=instructions,
                    answer_local_context=local_context,
                )
                instructions += "\n\nResearch context:\n" + expanded_context.prompt
                flow.answer_context = expanded_context.answer_context
                registry.update(full_registry)
                registry.pop("find_gif", None)
                expanded = plan_definition(registry)
                session.replace_tools((expanded,))
                # The provider resends the larger definition on subsequent rounds.
                budget.next_input_estimate += max(0, estimate_tokens(json.dumps(
                    expanded.parameters, ensure_ascii=False,
                )) - sum(estimate_tokens(json.dumps(item.parameters, ensure_ascii=False))
                         for item in definitions))
                context.state.family_catalogue = family_catalogue
                budget.request_limits = limits.FAMILY_LIMITS if family_catalogue else limits.RESEARCH_LIMITS
                rounder.social_route = False
                LOGGER.info("Agent route expanded: request=%s rounds=%s", request_id, rounder.rounds)
                return instructions

            rounder.expand_context = expand_context
        LOGGER.info("Agent route: request=%s route=%s", request_id,
                    "game_advice" if game_advice else "social" if social else "research")

        runner = PlanRunner(
            service=self,
            context=context,
            registry=registry,
            command_capabilities=command_capabilities,
            budget=budget,
            rounder=rounder,
        )

        flow = AnswerFlow(
            context=context,
            session=session,
            registry=registry,
            budget=budget,
            rounder=rounder,
            runner=runner,
            request_id=request_id,
            final_session_factory=self._model.create_agent_session,
            answer_context=compiled.answer_context,
            answer_images=images,
        )
        flow.fixed_system_prompt = system_prompt
        return flow, usage

    @staticmethod
    def _game_advice_context(question, local_context, conversation_history, context, system):
        prompt = json.dumps({'request': question, 'reply_context': context.state.reply_context,
            'nearby_conversation': local_context[-4000:], 'recent_conversation': conversation_history[-2000:]},
            ensure_ascii=False)
        return CompiledContext(prompt, estimate_tokens(system + prompt) + 512,
                               len((system + prompt).encode('utf-8')), (), 0, False)

    async def answer(
        self,
        *,
        question: str,
        local_context: str,
        context: AgentRequestContext,
        conversation_history: str = "",
    ) -> str:
        await request_scope(context, refresh=True)
        await require_evidence_access(context)
        if context.history:
            from ..conversation.preparation import prepare_history
            conversation_history = await prepare_history(
                context, context.history, context.state.authorized_checkpoint,
            )
        context.state.request_text = question
        context.state.resolved_members.clear()
        context.state.member_self_target = False
        await prepare_reply_context(context)
        if not context.private_pass:
            async def run_private():
                return await private_answer_pass(self, context, question, local_context)
            context.state.private_answer_run = run_private
        record = OperatorDebugRecord(question)
        context = replace(context, operator_debug=record)
        try:
            answer = await self._answer(
                question=question, local_context=local_context, context=context,
                conversation_history=conversation_history,
            )
            if not is_preview_reply(context, answer) and member_target_issue(context, answer):
                context.state.proposed_changes.clear()
                context.state.preview_reply = None
                answer = "Which member do you mean? Please mention them."
                record.status = "incomplete"
            if not answer.strip() and not (
                context.state.gif_url or context.state.attachments
                or context.state.reply_delivery.delivery.message_ids
            ):
                answer = preview_text(context.state.proposed_changes) if context.state.proposed_changes else unfinished_answer(question, context.state.grounding_evidence)
                record.status = "incomplete"
            answer = scoped_answer(context, answer)
            record.answer = answer
            return answer
        except asyncio.CancelledError:
            record.status = "cancelled"
            raise
        except Exception:
            if record.status == "incomplete":
                record.status = "failed"
            raise
        finally:
            log_scope(context)
            if context.transcript_archive is not None:
                try:
                    context.transcript_archive.enqueue_debug(context, record)
                except Exception:
                    LOGGER.exception("Agent operator debug archive enqueue failed")

    async def _answer(
        self,
        *,
        question: str,
        local_context: str,
        context: AgentRequestContext,
        conversation_history: str = "",
    ) -> str:
        from ..files.images import request_images
        context.state.request_text = question
        started_at = time.monotonic()
        images = await request_images(context)
        route = await judge_route(
            self._model, question=question, local_context=local_context,
            conversation_history=conversation_history, context=context, images=images,
        )
        if images and route.social:
            local_context += '\n<image_observation>\n' + json.dumps({
                'observation': route.image_observation,
                'status': 'observed' if route.image_observation else 'unavailable',
            }, ensure_ascii=False) + '\n</image_observation>'
        if route.game_advice and context.knowledge_store is not None:
            # A remit judgment must not bypass an answer in approved knowledge.
            # The research session sees the scoped index and can open relevant
            # sections before deciding whether tactical advice remains to decline.
            route = replace(route, game_advice=False)
        research_history = context.state.authorized_history
        research_checkpoint = context.state.authorized_checkpoint
        if route.social and research_history is not None:
            retained = []
            for turn in research_history[-4:]:
                if await can_disclose_provenance(context, turn.source_channels, turn.required_access):
                    retained.append(turn)
            context.state.authorized_history = tuple(retained)
        if route.social:
            context.state.authorized_checkpoint = None
        if context.knowledge_store is not None and not route.game_advice:
            context.state.knowledge_channel_access = await channel_knowledge_access(context)
        if not route.social and not route.game_advice:
            context.state.channel_directory = await scoped_channel_directory(context)
        context.operator_debug.image_labels = [image.label for image in images] + context.state.image_source_status
        flow, usage = self._start_flow(
            question=question,
            local_context=local_context,
            context=context,
            conversation_history=conversation_history,
            images=images,
            route=route,
            research_history=research_history,
            research_checkpoint=research_checkpoint,
        )
        status = "incomplete"
        rounder = flow.rounder
        runner = flow.runner
        try:
            answer = await flow.run()
            status = "completed"
            return answer
        except AgentGracefulEnd as error:
            await require_evidence_access(flow.context)
            status = "incomplete"
            if context.state.family_catalogue and isinstance(error, AgentLimitReached):
                context.state.terminal_cause = 'limit'
                return unfinished_answer(question, context.state.grounding_evidence)
            return str(error)
        except TextGenerationError as error:
            status = "provider_error"
            await require_evidence_access(context)
            if flow.read_results:
                return unfinished_answer(question, flow.model_results(flow.completed_results))
            raise AgentUnavailableError(str(error)) from error
        except PlanNotSettled as error:
            LOGGER.warning(
                "Agent plan not settled: request=%s reason=%s",
                getattr(flow.context.source_message, "id", None),
                error,
            )
            await require_evidence_access(flow.context)
            status = "incomplete"
            return unfinished_answer(question, context.state.grounding_evidence)
        except asyncio.CancelledError:
            status = "cancelled"
            raise
        except AgentAccessLost:
            status = "access_lost"
            raise
        finally:
            context.operator_debug.status = status
            self._log_completion(
                context=flow.context,
                rounds=rounder.rounds + 1,
                tool_calls=runner.tool_calls,
                evidence_characters=runner.evidence_characters,
                usage=usage,
                status=status,
                elapsed_ms=int((time.monotonic() - started_at) * 1000),
            )
