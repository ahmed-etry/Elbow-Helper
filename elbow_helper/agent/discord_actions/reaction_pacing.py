"""Offer occasional combo slots from this bot's own channel activity."""

from .reactions import reaction_key


COMBO_REPLY_INTERVAL = 2


def channel_combo_history(messages, bot_id, *, last_combo_message_id=0):
    """Pair bot reactions with following replies, oldest first.

    Reactions live on request messages. No member content is inspected. A
    pending combo also closes the slot before its accompanying reply arrives.
    """
    if bot_id is None:
        return None
    flags, pending, reacted = [], False, False
    for message in messages:
        own = {reaction_key(item.emoji) for item in getattr(message, "reactions", ())
               if getattr(item, "me", False)}
        reacted |= bool(own)
        if len(own) >= 2 or (last_combo_message_id and message.id == last_combo_message_id):
            pending = True
        if message.author.id == bot_id:
            if reacted or pending:
                flags.append(pending)
            pending = reacted = False
    if last_combo_message_id > max((item.id for item in messages), default=0):
        pending = True
    if pending:
        flags.append(True)
    return tuple(flags[-(COMBO_REPLY_INTERVAL - 1):])


def combo_slot_status(context):
    """Explain the metadata gate without inspecting member text."""
    history = context.state.channel_bot_combos
    if context.state.channel_reactions.emojis is None:
        return 'reaction_history_unavailable'
    if history is None:
        return 'reply_history_unavailable'
    if context.state.reaction_combo_revision != context.state.channel_reactions.combo_revision:
        return 'revision_changed'
    return 'cooldown' if any(history[-(COMBO_REPLY_INTERVAL - 1):]) else 'available'


def reply_combo_due(context):
    return combo_slot_status(context) == 'available'


def combo_slot_context(context):
    if context.state.reaction_combo_due:
        return ('Reaction combinations are available as an option. Choose the expression for this exchange. '
                'Each selected emoji must refer to the same point in the message and contribute '
                'to that response. An available combination does not need to be used. '
                'Keep standalone fallback text; a delivered image has no text.')
    return 'Reaction combinations are unavailable for this reply. Use an available response form.'
