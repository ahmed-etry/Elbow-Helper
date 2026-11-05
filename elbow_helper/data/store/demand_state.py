"""Checkpoints for profiles features ask for: each account joins its run only as it is stored.

Queuing every account when the run starts would take an account over from a scheduled pass
that is about to store its own fetch. ``players.collect_requested`` queues one at a time and
leaves such an account to the scheduled pass.
"""

from .player_state import finish, requested

__all__ = ("finish", "queue", "requested")


def queue(connection, run, specs, at):
    """Nothing is queued when the run starts."""
