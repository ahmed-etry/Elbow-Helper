"""Request contexts and completed turns retained for a conversation."""

from __future__ import annotations

import json
import logging
import sqlite3

from .state import ConversationRecord, ConversationTurn
from .instructions import WorkingState
from ..models import AgentRequestContext, AgentTurnState
from ..knowledge.report import KnowledgeReport

LOGGER = logging.getLogger(__name__)


class AgentTurnMixin:
    async def _record_action_outcome(self, context, run, message) -> None:
        root_id = context.conversation_root_id
        if root_id is None:
            return
        conversation = self._conversations.get(root_id)
        if conversation is None:
            return
        async with conversation.lock:
            conversation.append(ConversationTurn(
                text=json.dumps({
                    "action_run_id": run["run_id"],
                    "status": run["status"],
                    "actions": [{
                        "name": step["action_name"],
                        "label": step["action_label"],
                        "status": step["status"],
                        "targets": json.loads(step["values_json"]),
                    } for step in run["steps"]],
                }, ensure_ascii=False),
                source_channels=frozenset(context.state.source_channels),
                required_access=frozenset(context.state.required_access),
            ))
            if message is not None:
                self._conversations.register_reply(conversation, message.id)
            if self.persistence is not None:
                await self.persistence.save(self._conversations, conversation)

    def _request_context(self, message, member, referenced, conversation,
                         deadline_monotonic):
        root_id = (next(key for key, value in self._conversations.entries()
                        if value is conversation) if conversation is not None else None)
        context = AgentRequestContext(
            bot=self.bot,
            guild=message.guild,
            member=member,
            source_message=message,
            account_links=self.account_links,
            clan_health=self.clan_health,
            message_search=self.message_search,
            thread_discovery=self.thread_discovery,
            state=AgentTurnState(
                source_channels={message.channel.id},
                working=conversation.working if conversation is not None else WorkingState(),
            ),
            history=tuple(conversation.turns) if conversation is not None else (),
            roster_queries=self.roster_queries,
            cwl_queries=self.cwl_queries,
            war_queries=self.war_queries,
            transfer_queries=self.transfer_queries,
            hibernation_queries=self.hibernation_queries,
            support_queries=self.support_queries,
            recruitment_queries=self.recruitment_queries,
            examination_queries=self.examination_queries,
            record_queries=self.record_queries,
            achievement_queries=self.achievement_queries,
            event_queries=self.event_queries,
            member_lifecycle_queries=self.member_lifecycle_queries,
            clan_reporting_queries=self.clan_reporting_queries,
            role_connection_queries=self.role_connection_queries,
            knowledge_store=self.knowledge_store,
            research_jobs=self.research_jobs,
            action_repository=self.action_repository,
            action_runner=self.action_runner,
            conversation_root_id=root_id,
            attachment_sources=tuple(item for item in (message, referenced) if item is not None),
            deadline_monotonic=deadline_monotonic,
        )
        return context, root_id

    async def _record_turn(self, message, member, question, response,
                           local_context, context, delivery, conversation):
        if delivery.attempted_nonces:
            self._commit_reports(conversation, context.state)
            conversation.working = context.state.working
            delivered_answer = response if delivery.complete else "\n".join(delivery.text_parts)
            conversation.append(ConversationTurn(
                text=json.dumps({
                    "asker": member.display_name, "member_id": member.id,
                    "question": question, "answer": delivered_answer[:16_000],
                    "answer_truncated": len(delivered_answer) > 16_000,
                    "local_context": local_context[:12_000],
                    "lookup_excerpts": [item[:5_000] for item in context.state.evidence[-4:]],
                    "report_ids": list(context.state.reports),
                }, ensure_ascii=False),
                source_channels=frozenset(context.state.source_channels),
                required_access=frozenset(context.state.required_access),
                knowledge_refs=tuple(sorted(
                    (section.section_id, section.content_sha256)
                    for report in context.state.reports.values()
                    if isinstance(report, KnowledgeReport)
                    for section in report.sections
                )),
                record=ConversationRecord(
                    request_message_id=message.id,
                    member_id=member.id,
                    created_at=message.created_at.isoformat(),
                    question=question,
                    generated_answer=response,
                    delivered_answer=delivered_answer,
                    local_context=local_context,
                    evidence=tuple(context.state.evidence),
                    report_ids=tuple(context.state.reports),
                    reply_ids=tuple(delivery.message_ids),
                    delivery_complete=delivery.complete,
                    delivery_unknown=delivery.unknown,
                    attempted_nonces=tuple(delivery.attempted_nonces),
                    uncertain_nonce=delivery.uncertain_nonce,
                ),
            ))
            self._refresh_history_checkpoint(
                conversation, context.state,
                created_at=message.created_at,
                previous_turn_count=len(conversation.turns) - 1,
            )
            if self.persistence is not None:
                try:
                    await self.persistence.save(self._conversations, conversation)
                except (OSError, sqlite3.Error, RuntimeError, TypeError, ValueError):
                    LOGGER.exception("Agent checkpoint save failed: request=%s", message.id)
