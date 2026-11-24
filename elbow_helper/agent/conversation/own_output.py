"""What the bot's own confirmed changes and closed Confirm cards read like when they come back to the model."""

CARD_ENDS = {"cancelled": "Your Confirm card was cancelled", "expired": "Your Confirm card expired",
             "replaced": "Your Confirm card was replaced by a newer card",
             "unqueued": "Your Confirm couldn't start"}


def action_turn_text(data):
    """The line for a turn that records a run or a closed card, or None for any other turn."""
    if not isinstance(data, dict):
        return None
    if "action_run_id" in data:
        steps = "; ".join(
            f"{action.get('label')} ({action.get('status')})"
            + "".join(f" {link}" for link in action.get("target_links", ()))
            for action in data.get("actions", ()))
        return f"[Changes you ran after Confirm, {data.get('status')}: {steps}]"
    if data.get("confirmation") in CARD_ENDS:
        return (f"[{CARD_ENDS[data['confirmation']]}, nothing ran: "
                + ", ".join(data.get("actions", ())) + "]")
    return None
