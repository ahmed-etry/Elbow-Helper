"""Collect feature adapters without making features import the agent runtime."""

from elbow_helper.agent.features import account_links, achievements, attack_plans, clan_reporting, clan_transfers, cwl, diagnostics, event_stats as events, hibernation, leadership_news, recruitment, role_connections, rosters, support_tickets, wars
from elbow_helper.agent.features import clan_health, examination, member_lifecycle
from elbow_helper.agent.features.clan_health.commands import run_health_player
from elbow_helper.agent.features.recruitment.commands import run_opinion
FEATURES = {
    "clan_health": clan_health,
    "account_links": account_links,
    "achievements": achievements,
    "attack_plans": attack_plans,
    "clan_reporting": clan_reporting,
    "clan_transfers": clan_transfers,
    "cwl": cwl,
    "diagnostics": diagnostics,
    "events": events,
    "examination": examination,
    "hibernation": hibernation,
    "leadership_news": leadership_news,
    "member_lifecycle": member_lifecycle,
    "recruitment": recruitment,
    "role_connections": role_connections,
    "rosters": rosters,
    "support_tickets": support_tickets,
    "wars": wars,
}


def enabled_adapters():
    return tuple(adapter for feature in FEATURES.values()
                 for adapter in feature.COMMAND_ADAPTERS)


__all__ = ["FEATURES", "enabled_adapters", "run_health_player", "run_opinion"]
