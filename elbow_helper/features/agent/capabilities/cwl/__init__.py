"""Cwl agent capabilities."""

from .reads import cwl_tools
from .bonus_review import cwl_bonus_review_tools
from .bonus_scoring import cwl_bonus_scoring_tools
from .cc_status import cwl_cc_status_tools
from .prep_board import cwl_prep_refresh_tools
from .member_hub import cwl_member_hub_tools
from .bonus_export import cwl_bonus_adapters
from .brief import cwl_brief_adapters
from .thread_registration import cwl_register_adapters
from .roster_export import cwl_roster_adapters
from .announcement import cwl_announcement_adapters
from .transfer_reminder import cwl_transfer_reminder_adapters

TOOLS = (
    *cwl_tools(),
    *cwl_bonus_review_tools(),
    *cwl_bonus_scoring_tools(),
    *cwl_cc_status_tools(),
    *cwl_prep_refresh_tools(),
    *cwl_member_hub_tools(),
)

COMMAND_ADAPTERS = (
    *cwl_bonus_adapters(),
    *cwl_brief_adapters(),
    *cwl_register_adapters(),
    *cwl_roster_adapters(),
    *cwl_announcement_adapters(),
    *cwl_transfer_reminder_adapters(),
)

UNDO_HANDLERS = {}

__all__ = ["TOOLS", "COMMAND_ADAPTERS", "UNDO_HANDLERS"]
