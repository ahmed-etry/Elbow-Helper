"""Reject unknown league attack facts before calculating leadership scores."""

from collections import Counter


def require_scoring_evidence(wars, roster, attacks):
    counts = Counter((row.get("war_id"), row.get("clan_code"), row.get("player_tag")) for row in attacks)
    for war in wars:
        if war.get("state", "warEnded") != "warEnded":
            raise ValueError("Stored war evidence is unavailable.")
    for member in roster:
        key = member.get("war_id"), member.get("clan_code"), member.get("player_tag")
        # Direct scoring callers may supply only measured attacks. Stored counters,
        # when present, must agree; an explicitly unknown counter stays unknown.
        used = member.get("attacks_used", counts[key])
        expected = member.get("attacks_expected", 1)
        if (type(used) is not int or type(expected) is not int or not 0 <= used <= expected
                or counts[key] != used):
            raise ValueError("Stored war evidence is unavailable.")
    for attack in attacks:
        if attack.get("stars") is None or attack.get("destruction") is None:
            raise ValueError("Stored war evidence is unavailable.")
