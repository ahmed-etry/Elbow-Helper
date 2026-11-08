"""Cwl agent capabilities."""

from elbow_helper.agent.features.cwl.reads import cwl_tools
from elbow_helper.agent.features.cwl.bonus_review import cwl_bonus_review_tools
from elbow_helper.agent.features.cwl.bonus_scoring import cwl_bonus_scoring_tools
from elbow_helper.agent.features.cwl.cc_status import cwl_cc_status_tools
from elbow_helper.agent.features.cwl.prep_board import cwl_prep_refresh_tools
from elbow_helper.agent.features.cwl.member_hub import cwl_member_hub_tools
from elbow_helper.agent.features.cwl.bonus_export import cwl_bonus_adapters
from elbow_helper.agent.features.cwl.brief import cwl_brief_adapters
from elbow_helper.agent.features.cwl.thread_registration import cwl_register_adapters
from elbow_helper.agent.features.cwl.roster_export import cwl_roster_adapters
from elbow_helper.agent.features.cwl.announcement import cwl_announcement_adapters
from elbow_helper.agent.features.cwl.transfer_reminder import cwl_transfer_reminder_adapters
from elbow_helper.agent.actions.undo import merge_undo_handlers
from elbow_helper.agent.features.cwl.bonus_scoring import UNDO_HANDLERS as BONUS_SCORING_UNDO_HANDLERS
from elbow_helper.agent.features.cwl.cc_status import UNDO_HANDLERS as CC_STATUS_UNDO_HANDLERS


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


UNDO_HANDLERS = merge_undo_handlers(
    BONUS_SCORING_UNDO_HANDLERS,
    CC_STATUS_UNDO_HANDLERS,
)

__all__ = ["TOOLS", "COMMAND_ADAPTERS", "UNDO_HANDLERS"]
