"""Bind action targets to identities established during this request."""

from collections.abc import Mapping
import re


MEMBER_PROVENANCE_INSTRUCTION = (
    "Before targeting a member in an action or reply (including pings and roasts), "
    "use an explicit mention in this request, the author of the message the requester "
    "replied to, or find_discord_members with status=resolved in this request. "
    "Nearby speakers, old lookups, the owner and the asker are not defaults. "
    "Keep all supplied name parts in the lookup. If it returns needs_input, ask "
    "which member. When the requester refers to themself, set self_target=true in "
    "submit_request_plan or submit_social_reply; that flag authorizes only the requester's own member ID. "
    "On the social route, optionally supply member_query only for a named third party "
    "the reply is about. Never supply it for the asker or for unnamed social chat. "
    "Public identity clarifications use display names, usernames only for colliding "
    "display names, and never raw IDs."
)
_MENTION = re.compile(r"<@!?(\d+)>")
_MEMBER_FIELDS = frozenset({
    "member_id", "member_ids", "user_id", "user_ids", "recipient_ids", "dm_member_ids",
    "target_member_id", "target_member_ids",
})


def member_target_ids(arguments, scope=None):
    identifiers = set()

    def add(value):
        for identifier in value if isinstance(value, (list, tuple)) else (value,):
            if type(identifier) is int and identifier > 0:
                identifiers.add(identifier)

    def visit(value):
        if isinstance(value, str):
            identifiers.update(int(item) for item in _MENTION.findall(value))
        elif isinstance(value, Mapping):
            if value.get("target_type") == "member":
                add(value.get("target_id"))
            for field, item in value.items():
                if field in _MEMBER_FIELDS:
                    add(item)
                visit(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                visit(item)

    visit(arguments)
    for entity in (scope or {}).get("entities", ()):
        if entity["kind"] in ("discord_member", "discord_member_set"):
            add(entity["key"])
    return identifiers


def request_reply_author_id(context):
    message = getattr(context, "source_message", None)
    reference = getattr(message, "reference", None)
    if reference is None or getattr(reference, "channel_id", None) not in (
        None, message.channel.id,
    ):
        return None
    identifier = getattr(reference, "message_id", None)
    if type(identifier) is not int:
        return None
    # Only the request's actual reply reference grants provenance. Selecting a
    # different output reply target must not authorize that message's author.
    for row in getattr(getattr(context, "state", None), "local_messages", ()):
        if row["message_id"] == identifier:
            return row["author_id"]
    resolved = getattr(reference, "resolved", None)
    if (getattr(resolved, "id", None) == identifier
            and getattr(getattr(resolved, "channel", None), "id", None) == message.channel.id
            and getattr(getattr(resolved, "guild", None), "id", None) == context.guild.id):
        return getattr(getattr(resolved, "author", None), "id", None)
    return None


def member_target_issue(context, arguments, scope=None, *, self_target=None):
    """Return a model-facing refusal before a non-read action targets anyone."""
    identifiers = member_target_ids(arguments, scope)
    if not identifiers:
        return ""
    state = getattr(context, "state", None)
    message = getattr(context, "source_message", None)
    allowed = {identifier for identifier in getattr(message, "raw_mentions", ())
               if type(identifier) is int and identifier > 0}
    allowed.update(getattr(state, "resolved_members", {}))
    author_id = request_reply_author_id(context)
    if type(author_id) is int:
        allowed.add(author_id)
    flag = getattr(state, "member_self_target", False) if self_target is None else self_target
    if flag is True:
        allowed.add(context.member.id)
    missing = identifiers - allowed
    if missing:
        return (f"Member IDs {sorted(missing)} have no target provenance in this request. "
                "Resolve them first with find_discord_members (status=resolved), or ask "
                "which member. For a self reference, set self_target=true in submit_request_plan; "
                "it authorizes only the asker.")
    return ""
