"""Unicode identity comparisons shared by record readers and agent adapters."""

import unicodedata

from .player_tags import normalize_player_tag


def normalized_name(value):
    return ''.join(char for char in unicodedata.normalize('NFKC', value).casefold()
                   if char.isalnum())


def edit_distance(left, right):
    """Small spelling edits, including a transposed pair of characters."""
    previous, older = list(range(len(right) + 1)), None
    for index, char in enumerate(left, 1):
        current = [index]
        for slot, other in enumerate(right, 1):
            value = min(current[-1] + 1, previous[slot] + 1,
                        previous[slot - 1] + (char != other))
            if (older is not None and slot > 1 and char == right[slot - 2]
                    and left[index - 2] == other):
                value = min(value, older[slot - 2] + 1)
            current.append(value)
        older, previous = previous, current
    return previous[-1]


def matching_accounts(query, accounts):
    """Prefer exact names, then word prefixes; allow small edits per word."""
    if query.strip().startswith('#'):
        tag = normalize_player_tag(query)
        return [accounts[tag]] if tag in accounts else []
    target = normalized_name(query)
    if not target:
        return []
    exact, prefix, fuzzy = [], [], []
    for account in accounts.values():
        aliases = [normalized_name(name) for name in account['names']]
        if target in aliases:
            exact.append(account)
        elif any(word_prefix(query, name) for name in account['names']):
            prefix.append(account)
        elif any(small_spelling_edit(query, name) for name in account['names']):
            fuzzy.append(account)
    return sorted(exact or prefix or fuzzy, key=lambda row: row['player_tag'])


def name_segments(value):
    return ''.join(char if char.isalnum() else ' '
                   for char in unicodedata.normalize('NFKC', value).casefold()).split()


def word_prefix(query, name):
    left, right = name_segments(query), name_segments(name)
    return bool(left) and len(left) == len(right) and all(
        other.startswith(part) for part, other in zip(left, right))


def small_spelling_edit(query, name):
    # Formatting-only changes resolve through normalized exact matching above.
    # For spelling changes, align every word and budget its edits independently;
    # a long word's allowance must not absorb another word or an initial.
    left, right = name_segments(query), name_segments(name)
    return bool(left) and len(left) == len(right) and all(
        edit_distance(part, other) <= (1 if max(len(part), len(other)) <= 5 else 2)
        for part, other in zip(left, right))


def name_parts(value):
    return tuple(name_segments(value))


def identity_query(query):
    """Separate explicit Discord mentions from a supplied identity label."""
    if query.isdecimal():
        return {int(query)}, ''
    identifiers, labels = set(), []
    while '<@' in query:
        before, _, tail = query.partition('<@')
        value, close, rest = tail.partition('>')
        numeric = value.removeprefix('!')
        if not close or not numeric.isdecimal():
            labels.extend((before, '<@', tail))
            query = ''
            break
        identifiers.add(int(numeric))
        labels.append(before)
        query = rest
    labels.append(query)
    return identifiers, ' '.join(labels)


def discord_names(member):
    names = [getattr(member, field, None)
             for field in ("display_name", "name", "global_name", "nick")]
    aliases = getattr(member, "aliases", ()) or ()
    names.extend((aliases,) if isinstance(aliases, str) else aliases)
    return tuple(dict.fromkeys(name for name in names if isinstance(name, str) and name))


def matches_discord_identity(member, query):
    """Check a cached identity without a guild chunk or account lookup."""
    identifiers, label = identity_query(query)
    if identifiers and identifiers != {member.id}:
        return False
    parts = name_parts(label)
    words = {word for name in discord_names(member) for word in name_parts(name)}
    return bool(identifiers or parts) and all(
        any(word.startswith(part) for word in words) for part in parts
    )


