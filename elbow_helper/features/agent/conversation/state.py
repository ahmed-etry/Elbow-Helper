"""Bounded, process-local conversations identified by their Discord replies."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass, field, replace
import hashlib
import json
import logging
import time
from ..reports.base import ReportArtifact
from .instructions import WorkingState


CONVERSATION_IDLE_SECONDS = 6 * 60 * 60
MAX_CONVERSATIONS = 128
MAX_RETAINED_BYTES = 1024 * 1024
MAX_RETAINED_TURNS = 2048
MAX_REPLY_REFERENCES = 512
MAX_CHECKPOINT_BYTES = 64 * 1024
MAX_CHECKPOINT_REQUEST_IDS = 512
LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ConversationRecord:
    """Complete permitted turn data, independent of the model-facing excerpt."""

    request_message_id: int
    member_id: int
    created_at: str
    question: str
    generated_answer: str
    delivered_answer: str
    local_context: str
    evidence: tuple[str, ...]
    report_ids: tuple[str, ...]
    reply_ids: tuple[int, ...]
    delivery_complete: bool
    delivery_unknown: bool = False
    attempted_nonces: tuple[int, ...] = ()
    uncertain_nonce: int | None = None
    retained_bytes: int = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if (
            type(self.request_message_id) is not int
            or self.request_message_id <= 0
            or type(self.member_id) is not int or self.member_id <= 0
            or type(self.delivery_complete) is not bool
            or type(self.delivery_unknown) is not bool
            or self.delivery_complete and self.delivery_unknown
            or not isinstance(self.reply_ids, tuple)
            or len(self.reply_ids) != len(set(self.reply_ids))
            or any(type(value) is not int or value <= 0 for value in self.reply_ids)
            or not isinstance(self.attempted_nonces, tuple)
            or len(self.attempted_nonces) != len(set(self.attempted_nonces))
            or any(
                type(value) is not int or not 0 <= value < 2 ** 63
                for value in self.attempted_nonces
            )
            or (self.uncertain_nonce is None) != (not self.delivery_unknown)
            or (
                self.uncertain_nonce is not None
                and self.uncertain_nonce not in self.attempted_nonces
            )
        ):
            raise ValueError("Invalid conversation delivery record")
        values = (
            self.created_at, self.question, self.generated_answer, self.delivered_answer,
            self.local_context, *self.evidence, *self.report_ids,
        )
        size = sum(len(value.encode("utf-8")) for value in values) + 8 * (
            2 + len(self.reply_ids) + len(self.attempted_nonces)
        )
        object.__setattr__(self, "retained_bytes", size)


@dataclass(frozen=True, slots=True)
class ConversationTurn:
    text: str
    source_channels: frozenset[int]
    record: ConversationRecord | None = None
    retention_limited: bool = False
    required_access: frozenset[str] = frozenset()
    knowledge_refs: tuple[tuple[str, str], ...] = ()
    retained_bytes: int = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        size = (
            len(self.text.encode("utf-8")) + 8 * len(self.source_channels)
            + sum(len(value.encode("utf-8")) for value in self.required_access)
            + (self.record.retained_bytes if self.record else 0)
            + sum(len(section_id) + len(digest)
                  for section_id, digest in self.knowledge_refs)
        )
        if (
            len(self.knowledge_refs) > 20
            or len(set(self.knowledge_refs)) != len(self.knowledge_refs)
            or any(
                not isinstance(section_id, str) or not section_id
                or len(section_id) > 100
                or not isinstance(digest, str) or len(digest) != 64
                or any(character not in "0123456789abcdef" for character in digest)
                for section_id, digest in self.knowledge_refs
            )
        ):
            raise ValueError("Invalid conversation knowledge references")
        object.__setattr__(self, "retained_bytes", size)


@dataclass(frozen=True, slots=True)
class ConversationCheckpoint:
    covered_turn_count: int
    covered_request_ids: tuple[int, ...]
    summary: str
    source_channels: frozenset[int]
    required_access: frozenset[str]
    input_hash: str
    created_at: str
    format_version: str = "deterministic_record_digest_v1"

    def __post_init__(self) -> None:
        if (
            type(self.covered_turn_count) is not int
            or self.covered_turn_count < 1
            or len(self.covered_request_ids) > MAX_CHECKPOINT_REQUEST_IDS
            or len(self.covered_request_ids) != len(set(self.covered_request_ids))
            or any(type(value) is not int or value <= 0 for value in self.covered_request_ids)
            or not self.summary
            or len(self.summary.encode("utf-8")) > MAX_CHECKPOINT_BYTES
            or not self.source_channels
            or any(type(value) is not int or value <= 0 for value in self.source_channels)
            or not isinstance(self.input_hash, str)
            or len(self.input_hash) != 64
            or any(character not in "0123456789abcdef" for character in self.input_hash)
            or not isinstance(self.created_at, str)
            or not self.created_at
            or self.format_version != "deterministic_record_digest_v1"
        ):
            raise ValueError("Invalid conversation checkpoint")


def checkpoint_input_hash(turns: tuple[ConversationTurn, ...]) -> str:
    payload = json.dumps([{
        "text": turn.text,
        "source_channels": sorted(turn.source_channels),
        "required_access": sorted(turn.required_access),
        "knowledge_refs": turn.knowledge_refs,
        "request_message_id": (
            turn.record.request_message_id if turn.record else None
        ),
    } for turn in turns], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(slots=True)
class Conversation:
    guild_id: int
    channel_id: int
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    turns: list[ConversationTurn] = field(default_factory=list)
    reports: dict[str, ReportArtifact] = field(default_factory=dict)
    report_sources: dict[str, frozenset[int]] = field(default_factory=dict)
    report_access_requirements: dict[str, frozenset[str]] = field(default_factory=dict)
    reply_ids: list[int] = field(default_factory=list)
    pending: int = 0
    touched_at: float = field(default_factory=time.monotonic)
    version: int = 0
    evicted_turns: int = 0
    working: WorkingState = field(default_factory=WorkingState)
    checkpoint: ConversationCheckpoint | None = None

    def append(self, turn: ConversationTurn) -> None:
        if turn.retained_bytes > MAX_RETAINED_BYTES:
            # Keep the bounded context excerpt if the full record is too large.
            # Never damage JSON by slicing its serialized text.
            turn = ConversationTurn(
                turn.text, turn.source_channels, retention_limited=True,
                required_access=turn.required_access,
                knowledge_refs=turn.knowledge_refs,
            )
            LOGGER.warning("Agent turn exceeds full-record retention budget")
        if turn.retained_bytes > MAX_RETAINED_BYTES:
            LOGGER.warning("Agent turn excerpt exceeds retention budget")
            return
        self.turns.append(turn)
        retained_bytes = sum(item.retained_bytes for item in self.turns)
        while len(self.turns) > MAX_RETAINED_TURNS or retained_bytes > MAX_RETAINED_BYTES:
            retained_bytes -= self.turns[0].retained_bytes
            self.turns.pop(0)
            self.evicted_turns += 1
        if self.checkpoint is not None and (
            self.checkpoint.covered_turn_count > len(self.turns)
            or checkpoint_input_hash(tuple(
                self.turns[:self.checkpoint.covered_turn_count]
            )) != self.checkpoint.input_hash
        ):
            self.checkpoint = None
        self.version += 1
        self.touched_at = time.monotonic()

    def record_for_request(self, message_id: int) -> ConversationTurn | None:
        """Internal lookup; callers must authorize the returned source channels."""
        return next((turn for turn in self.turns
                     if turn.record and turn.record.request_message_id == message_id), None)

    def reconcile_unknown_delivery(
        self, *, request_message_id: int, reply_id: int, nonce: int,
        delivered_part: str, delivery_complete: bool,
    ) -> ConversationRecord:
        """Confirm one previously uncertain Discord send without regenerating it."""
        matches = [
            (index, turn) for index, turn in enumerate(self.turns)
            if turn.record
            and turn.record.request_message_id == request_message_id
        ]
        if len(matches) != 1:
            raise ValueError("Unknown delivery request is not retained exactly once")
        index, turn = matches[0]
        record = turn.record
        if (
            not record.delivery_unknown
            or record.uncertain_nonce != nonce
            or reply_id in record.reply_ids
            or type(reply_id) is not int or reply_id <= 0
            or not isinstance(delivered_part, str)
            or type(delivery_complete) is not bool
            or delivery_complete
            and len(record.reply_ids) + 1 != len(record.attempted_nonces)
        ):
            raise ValueError("Delivery cannot be reconciled with this record")
        delivered_answer = record.delivered_answer
        if delivered_part:
            delivered_answer = (
                f"{delivered_answer}\n{delivered_part}"
                if delivered_answer else delivered_part
            )
        updated = replace(
            record, delivered_answer=delivered_answer,
            reply_ids=(*record.reply_ids, reply_id),
            delivery_complete=delivery_complete,
            delivery_unknown=False, uncertain_nonce=None,
        )
        self.turns[index] = replace(turn, record=updated)
        if self.checkpoint is not None and index < self.checkpoint.covered_turn_count:
            self.checkpoint = None
        self.version += 1
        self.touched_at = time.monotonic()
        return updated


class ConversationStore:
    def __init__(self):
        self._conversations: OrderedDict[int, Conversation] = OrderedDict()
        self._replies: dict[tuple[int, int, int], int] = {}

    def find(self, guild_id: int, channel_id: int, reply_id: int | None) -> Conversation | None:
        self._prune()
        key = self._replies.get((guild_id, channel_id, reply_id))
        return self._conversations.get(key)

    def create(self, guild_id: int, channel_id: int, message_id: int) -> Conversation:
        self._prune()
        existing = self._conversations.get(message_id)
        if existing is not None:
            if (existing.guild_id, existing.channel_id) != (guild_id, channel_id):
                raise ValueError("Conversation root identity conflicts with its scope")
            return existing
        if len(self._conversations) >= MAX_CONVERSATIONS:
            for key, value in self._conversations.items():
                if not self._is_active(value):
                    self._drop(key)
                    break
            else:
                raise RuntimeError("Agent conversation capacity reached")
        result = Conversation(guild_id, channel_id)
        self._conversations[message_id] = result
        return result

    def register_reply(self, conversation: Conversation, message_id: int) -> None:
        key = next(key for key, value in self._conversations.items() if value is conversation)
        reply_key = (conversation.guild_id, conversation.channel_id, message_id)
        if reply_key in self._replies:
            if self._replies[reply_key] != key:
                raise ValueError("Reply already belongs to another conversation")
            return
        self._replies[(conversation.guild_id, conversation.channel_id, message_id)] = key
        conversation.reply_ids.append(message_id)
        while len(conversation.reply_ids) > MAX_REPLY_REFERENCES:
            expired = conversation.reply_ids.pop(0)
            self._replies.pop((conversation.guild_id, conversation.channel_id, expired), None)
        conversation.touched_at = time.monotonic()
        self._conversations.move_to_end(key)

    def restore(self, root_message_id: int, conversation: Conversation) -> None:
        """Install decoded state without refreshing its retention lifetime.

        Validate identities before mutating indexes. Never replace a conversation
        already owned by running handlers with a stale disk copy.
        """
        if root_message_id in self._conversations:
            raise ValueError("Conversation root is already loaded")
        if conversation.pending or conversation.lock.locked():
            raise ValueError("Cannot restore an active conversation")
        if time.monotonic() - conversation.touched_at >= CONVERSATION_IDLE_SECONDS:
            raise ValueError("Cannot restore an expired conversation")
        if len(conversation.reply_ids) > MAX_REPLY_REFERENCES or len(set(conversation.reply_ids)) != len(conversation.reply_ids):
            raise ValueError("Invalid restored reply identities")
        identities = [(conversation.guild_id, conversation.channel_id, reply_id) for reply_id in conversation.reply_ids]
        if any(identity in self._replies for identity in identities):
            raise ValueError("Restored reply already belongs to a loaded conversation")
        self._prune()
        if len(self._conversations) >= MAX_CONVERSATIONS:
            for key, value in self._conversations.items():
                if not self._is_active(value):
                    self._drop(key)
                    break
            else:
                raise RuntimeError("Conversation restore capacity reached")
        self._conversations[root_message_id] = conversation
        self._replies.update((identity, root_message_id) for identity in identities)

    def entries(self) -> tuple[tuple[int, Conversation], ...]:
        """Snapshot identities for bounded persistence/cleanup coordination."""
        return tuple(self._conversations.items())

    def protected_roots(self) -> tuple[int, ...]:
        return tuple(key for key, value in self._conversations.items() if self._is_active(value))

    def loaded_roots(self) -> tuple[int, ...]:
        """Return live memory identities after applying normal idle expiry."""
        self._prune()
        return tuple(self._conversations)

    def _prune(self) -> None:
        now = time.monotonic()
        for key, value in list(self._conversations.items()):
            if (
                not self._is_active(value)
                and now - value.touched_at >= CONVERSATION_IDLE_SECONDS
            ):
                self._drop(key)

    @staticmethod
    def _is_active(conversation: Conversation) -> bool:
        return bool(conversation.pending or conversation.lock.locked())

    def _drop(self, key: int) -> None:
        conversation = self._conversations.pop(key)
        for reply_id in conversation.reply_ids:
            self._replies.pop((conversation.guild_id, conversation.channel_id, reply_id), None)
