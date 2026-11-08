"""Participant counts and rendering for ticket transcript summaries."""


def transcript_participants(messages):
    counts = {}
    labels = {}
    for message in messages:
        author = message.author
        counts[author.id] = counts.get(author.id, 0) + 1
        labels.setdefault(author.id, f"{author.mention} - {getattr(author, 'display_name', author.name)}")
    sorted_users = sorted(counts.items(), key=lambda item: item[1], reverse=True)
    top_users = sorted_users[:5]
    lines = []
    for user_id, count in top_users:
        label = labels.get(user_id, f"<@{user_id}>")
        message_count = "1 message" if count == 1 else f"{count} messages"
        lines.append(f"{label} — {message_count}")
    if len(sorted_users) > len(top_users):
        additional = len(sorted_users) - len(top_users)
        lines.append("+1 more participant" if additional == 1 else f"+{additional} more participants")
    return top_users, "\n".join(lines) if lines else "No participants"
