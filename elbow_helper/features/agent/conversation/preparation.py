"""Authorize retained evidence and prepare conversation context for each asker."""

from __future__ import annotations
import logging

from ..access import accessible_message_channel
from ..access import has_access_requirements
from ..models import AgentRequestContext, AgentTurnState
from ..reports.base import retain_report
from .state import Conversation
from .context import CHECKPOINT_MIN_GROWTH, CHECKPOINT_MIN_OMITTED_TURNS, build_history_checkpoint

LOGGER = logging.getLogger("elbow_helper.features.agent.cog")


class ConversationContextMixin:
    """Agent context preparation; never calls the provider or sends replies."""

    async def _conversation_history(self, conversation: Conversation, context: AgentRequestContext) -> str:
        access: dict[int, bool] = {}
        retained = []
        for turn in conversation.turns:
            for channel_id in turn.source_channels:
                if channel_id not in access:
                    access[channel_id] = await accessible_message_channel(context, channel_id) is not None
            if (all(access[channel_id] for channel_id in turn.source_channels)
                    and (not turn.required_access or has_access_requirements(
                        context.guild, context.member.id, turn.required_access,
                    ))):
                retained.append(turn)
        context.state.authorized_history = tuple(retained)
        checkpoint = conversation.checkpoint
        if checkpoint is not None:
            for channel_id in checkpoint.source_channels:
                if channel_id not in access:
                    access[channel_id] = (
                        await accessible_message_channel(context, channel_id)
                        is not None
                    )
            if (
                all(access[channel_id] for channel_id in checkpoint.source_channels)
                and has_access_requirements(
                    context.guild, context.member.id,
                    checkpoint.required_access,
                )
            ):
                context.state.authorized_checkpoint = checkpoint
        instructions = []
        for instruction in context.state.working.instructions:
            if not instruction.active:
                continue
            channel_id = instruction.source_channel_id
            if channel_id not in access:
                access[channel_id] = await accessible_message_channel(context, channel_id) is not None
            if access[channel_id]:
                instructions.append(instruction)
        context.state.authorized_instructions = tuple(instructions)
        context.state.history_status = {
            "included_turns": len(retained),
            "older_retained_turns_available": 0,
            "history_may_be_incomplete": True,
        }
        # Compilation selects whole records using all request components. Keep
        # this authorized transcript available to alternate service callers.
        return "\n".join(turn.text for turn in retained)

    @staticmethod
    def _refresh_history_checkpoint(
        conversation: Conversation, state: AgentTurnState, *,
        created_at, previous_turn_count: int,
    ) -> None:
        omitted = int(state.history_status.get(
            "older_retained_turns_available", 0,
        ))
        checkpoint = conversation.checkpoint
        fully_authorized = (
            state.authorized_history is not None
            and len(state.authorized_history) == previous_turn_count
        )
        if (
            fully_authorized
            and omitted >= CHECKPOINT_MIN_OMITTED_TURNS
            and (
                checkpoint is None
                or omitted - checkpoint.covered_turn_count
                >= CHECKPOINT_MIN_GROWTH
            )
        ):
            conversation.checkpoint = build_history_checkpoint(
                conversation.turns, covered_turn_count=omitted,
                created_at=created_at,
            )

    async def _load_authorized_reports(
        self,
        conversation: Conversation,
        context: AgentRequestContext,
    ) -> None:
        """Expose retained artifacts only while all original access still holds."""
        access: dict[int, bool] = {}
        for report_id, report in conversation.reports.items():
            sources = conversation.report_sources.get(report_id)
            requirements = conversation.report_access_requirements.get(report_id)
            if sources is None or requirements is None:
                LOGGER.warning(
                    "Dropping retained agent report without provenance: report=%s",
                    report_id,
                )
                continue
            for channel_id in sources:
                if channel_id not in access:
                    access[channel_id] = (
                        await accessible_message_channel(context, channel_id)
                        is not None
                    )
            authorized = (
                all(access[channel_id] for channel_id in sources)
                and has_access_requirements(
                    context.guild, context.member.id, requirements,
                )
            )
            if authorized:
                context.state.reports[report_id] = report
                context.state.report_sources[report_id] = sources
                context.state.report_access_requirements[report_id] = requirements
                context.state.source_channels.update(sources)
                context.state.required_access.update(requirements)
            else:
                context.state.preserved_reports[report_id] = report
                context.state.preserved_report_sources[report_id] = sources
                context.state.preserved_report_access_requirements[report_id] = requirements

    @staticmethod
    def _commit_reports(conversation: Conversation, state: AgentTurnState) -> None:
        """Merge hidden and visible artifacts without exposing or erasing either."""
        reports = {**state.preserved_reports, **state.reports}
        sources = {**state.preserved_report_sources, **state.report_sources}
        requirements = {
            **state.preserved_report_access_requirements,
            **state.report_access_requirements,
        }
        ordered_ids = [
            report_id for report_id in conversation.reports if report_id in reports
        ]
        ordered_ids.extend(
            report_id for report_id in reports if report_id not in ordered_ids
        )
        retained = {}
        for report_id in ordered_ids:
            if report_id in sources and report_id in requirements:
                retain_report(retained, reports[report_id])
        conversation.reports = retained
        conversation.report_sources = {
            report_id: sources[report_id] for report_id in retained
        }
        conversation.report_access_requirements = {
            report_id: requirements[report_id] for report_id in retained
        }
