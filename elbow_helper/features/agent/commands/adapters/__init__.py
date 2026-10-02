"""Feature-owned slash command adapters."""

from __future__ import annotations

from ...capabilities.clan_health.commands import health_adapters, run_health_player
from ...capabilities.recruitment.commands import recruitment_adapters, run_opinion
from ...capabilities.records.commands import record_adapters
from ...capabilities.events.commands import event_adapters
from ...capabilities.diagnostics.commands import diagnostic_adapters
from ...capabilities.cwl.bonus_export import cwl_bonus_adapters
from ...capabilities.role_connections.commands import role_connection_adapters
from ...capabilities.cwl.brief import cwl_brief_adapters
from ...capabilities.achievements.commands import achievement_adapters
from ...capabilities.achievements.raffle_commands import raffle_adapters
from ...capabilities.account_links.commands import account_link_adapters
from ...capabilities.wars.statements import war_statement_adapters
from ...capabilities.cwl.thread_registration import cwl_register_adapters
from ...capabilities.cwl.roster_export import cwl_roster_adapters
from ...capabilities.support_tickets.commands import support_ticket_adapters
from ...capabilities.attack_plans.commands import attack_plan_adapters
from ...capabilities.clan_transfers.commands import clan_transfer_adapters
from ...capabilities.rosters.setup_commands import roster_adapters
from ...capabilities.rosters.timing_commands import timing_adapters
from ...capabilities.rosters.post_commands import post_adapters
from ...capabilities.cwl.announcement import cwl_announcement_adapters
from ...capabilities.hibernation.commands import hibernation_adapters
from ...capabilities.cwl.transfer_reminder import cwl_transfer_reminder_adapters


def enabled_adapters():
    return (*recruitment_adapters(), *health_adapters(), *record_adapters(),
            *event_adapters(), *diagnostic_adapters(), *cwl_bonus_adapters(),
            *role_connection_adapters(), *cwl_brief_adapters(),
            *achievement_adapters(), *raffle_adapters(), *account_link_adapters(),
            *war_statement_adapters(), *cwl_register_adapters(),
            *cwl_roster_adapters(), *support_ticket_adapters(),
            *attack_plan_adapters(), *clan_transfer_adapters(),
            *roster_adapters(), *timing_adapters(), *post_adapters(), *cwl_announcement_adapters(),
            *hibernation_adapters(), *cwl_transfer_reminder_adapters())


__all__ = ["enabled_adapters", "run_health_player", "run_opinion"]
