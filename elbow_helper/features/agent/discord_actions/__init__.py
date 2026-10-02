"""Discord changes prepared for confirmation."""

from ..actions.undo import merge_undo_handlers
from .roles import UNDO_HANDLERS as ROLES_UNDO_HANDLERS
from .messages import UNDO_HANDLERS as MESSAGES_UNDO_HANDLERS
from .threads import UNDO_HANDLERS as THREADS_UNDO_HANDLERS
from .message_controls import UNDO_HANDLERS as MESSAGE_CONTROLS_UNDO_HANDLERS
from .nicknames import UNDO_HANDLERS as NICKNAMES_UNDO_HANDLERS

UNDO_HANDLERS = merge_undo_handlers(
    ROLES_UNDO_HANDLERS,
    MESSAGES_UNDO_HANDLERS,
    THREADS_UNDO_HANDLERS,
    MESSAGE_CONTROLS_UNDO_HANDLERS,
    NICKNAMES_UNDO_HANDLERS,
)

__all__ = ["UNDO_HANDLERS"]
