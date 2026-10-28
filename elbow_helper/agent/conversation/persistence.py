"""Off-loop checkpoint coordination for bounded active conversation state."""

import asyncio
import logging
import time

from .state import Conversation, ConversationStore, MAX_CONVERSATIONS
from .repository import ConversationRepository
from .codec import decode_conversation, encode_conversation


LOGGER = logging.getLogger(__name__)


class ConversationPersistence:
    def __init__(self, repository: ConversationRepository):
        self.repository = repository
        self._revisions: dict[int, int] = {}
        self._lock = asyncio.Lock()

    async def restore(self, store: ConversationStore, *, guild_id: int) -> None:
        async with self._lock:
            now = time.time()
            await asyncio.to_thread(
                self.repository.prune, now=now,
                protected_roots=store.protected_roots(),
            )
            snapshots = await asyncio.to_thread(self.repository.load_active, now=now, limit=MAX_CONVERSATIONS)
            for snapshot in snapshots:
                if snapshot.guild_id != guild_id:
                    continue
                try:
                    conversation = decode_conversation(snapshot)
                    store.restore(snapshot.root_message_id, conversation)
                except (KeyError, TypeError, ValueError, RuntimeError):
                    LOGGER.exception("Agent checkpoint could not be restored: root=%s", snapshot.root_message_id)
                    continue
                self._revisions[snapshot.root_message_id] = snapshot.revision

    async def restore_reply(
        self, store: ConversationStore, *, guild_id: int, channel_id: int,
        reply_id: int,
    ) -> Conversation | None:
        """Restore one evicted conversation addressed by an exact reply."""

        async with self._lock:
            existing = store.find(guild_id, channel_id, reply_id)
            if existing is not None:
                return existing
            snapshot = await asyncio.to_thread(
                self.repository.find_reply, guild_id, channel_id, reply_id,
                now=time.time(),
            )
            if snapshot is None:
                return None
            try:
                conversation = decode_conversation(snapshot)
                store.restore(snapshot.root_message_id, conversation)
            except (KeyError, TypeError, ValueError, RuntimeError):
                LOGGER.exception(
                    "Agent reply checkpoint could not be restored: root=%s",
                    snapshot.root_message_id,
                )
                return None
            self._revisions[snapshot.root_message_id] = snapshot.revision
            return conversation

    async def save(self, store: ConversationStore, conversation: Conversation) -> None:
        # Hold the conversation lock in the caller. This lock serializes disk
        # writes and cleanup; a cancelled caller must let its started write settle.
        async def persist():
            async with self._lock:
                root = next(key for key, item in store.entries() if item is conversation)
                snapshot = encode_conversation(conversation, root_message_id=root,
                                               revision=self._revisions.get(root, 0))
                revision = await asyncio.to_thread(self.repository.save, snapshot, tuple(conversation.reply_ids),
                                                   protected_roots=store.loaded_roots())
                self._revisions[root] = revision
                loaded = {key for key, _ in store.entries()}
                self._revisions = {key: value for key, value in self._revisions.items() if key in loaded}

        task = asyncio.create_task(persist())
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise

    async def prune(self, store: ConversationStore) -> None:
        async with self._lock:
            await asyncio.to_thread(self.repository.prune, now=time.time(), protected_roots=store.protected_roots())
