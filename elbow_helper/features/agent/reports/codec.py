"""Select typed reconstruction for a retained conversation report."""

from typing import Any

from .community_codec import (
    decode_achievement_progress,
    decode_event_schedule,
    decode_member_lifecycle,
    decode_active_hibernation,
    decode_support_ticket_inventory,
    decode_active_recruitment_trials,
    decode_examination_case_status,
    decode_discord_research,
)
from .clash_codec import (
    decode_missing_elder,
    decode_cwl_performance,
    decode_pending_transfer_requests,
)
from .attachment_codec import (
    decode_attachment,
)


def decode_report(row: dict[str, Any], *, guild_id: int) -> Any:
    """Reconstruct one typed report while enforcing its guild boundary."""
    decoder = _DECODERS.get(row["kind"])
    if decoder is None:
        raise ValueError("Unsupported conversation report kind")
    return decoder(row, guild_id)


_DECODERS = {
    "achievement_progress": decode_achievement_progress,
    "event_schedule": decode_event_schedule,
    "member_lifecycle": decode_member_lifecycle,
    "missing_elder": decode_missing_elder,
    "cwl_performance": decode_cwl_performance,
    "pending_transfer_requests": decode_pending_transfer_requests,
    "active_hibernation": decode_active_hibernation,
    "support_ticket_inventory": decode_support_ticket_inventory,
    "active_recruitment_trials": decode_active_recruitment_trials,
    "examination_case_status": decode_examination_case_status,
    "csv_import": decode_attachment,
    "xlsx_import": decode_attachment,
    "text_import": decode_attachment,
    "discord_research": decode_discord_research,
}

__all__ = ["decode_report"]
