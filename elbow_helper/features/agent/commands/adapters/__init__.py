"""Feature-owned slash command adapters."""

from __future__ import annotations

from .clan_health import health_adapters, run_health_player
from .recruitment import recruitment_adapters, run_opinion
from .records import record_adapters
from .event_stats import event_adapters
from .diagnostics import diagnostic_adapters
from .cwl_bonus import cwl_bonus_adapters
from .role_connections import role_connection_adapters
from .cwl_brief import cwl_brief_adapters
from .achievements import achievement_adapters
from .account_links import account_link_adapters
from .war_statements import war_statement_adapters
from .cwl_register import cwl_register_adapters
from .cwl_roster import cwl_roster_adapters
from .support_tickets import support_ticket_adapters
from .attack_plans import attack_plan_adapters
from .clan_transfers import clan_transfer_adapters
from .rosters import roster_adapters
from .cwl_announcement import cwl_announcement_adapters
from .hibernation import hibernation_adapters
from .cwl_transfer_reminder import cwl_transfer_reminder_adapters


def enabled_adapters():
    return (*recruitment_adapters(), *health_adapters(), *record_adapters(),
            *event_adapters(), *diagnostic_adapters(), *cwl_bonus_adapters(),
            *role_connection_adapters(), *cwl_brief_adapters(),
            *achievement_adapters(), *account_link_adapters(),
            *war_statement_adapters(), *cwl_register_adapters(),
            *cwl_roster_adapters(), *support_ticket_adapters(),
            *attack_plan_adapters(), *clan_transfer_adapters(),
            *roster_adapters(), *cwl_announcement_adapters(),
            *hibernation_adapters(), *cwl_transfer_reminder_adapters())


__all__ = ["enabled_adapters", "run_health_player", "run_opinion"]
