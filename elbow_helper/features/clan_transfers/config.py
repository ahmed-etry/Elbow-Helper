"""Clan transfer routing and shared config."""

from pathlib import Path

from discord import app_commands

from elbow_helper.configuration.clans import CLAN_MEMBER_ROLE_IDS

CLAN_TRANSFER_STATE_FILE = Path("data/clan_transfers/transfer_queue.json")
REQUEST_TTL_HOURS = 12
CWL_SEASON_TRANSFER_CLANS = frozenset({"BEE", "BEP"})

# Per-clan queue thread routing.
CLAN_TRANSFER_THREADS = {
    "BEH": 1450676506769625159,
    "BE4": 1450676608695406654,
    "BES": 1450676680136986624,
    "BE1": 1450676720725266432,
    "BEM": 1450676755475202211,
    "BEC": 1450676793504694413,
    "BEE": 1450676861813264444,
    "BEP": 1450676899939483841,
}

CLAN_TRANSFER_QUEUES = {
    code: {"thread_id": thread_id, "role_id": CLAN_MEMBER_ROLE_IDS[code]}
    for code, thread_id in CLAN_TRANSFER_THREADS.items()
}

CLAN_CHOICES = [app_commands.Choice(name=code, value=code) for code in CLAN_TRANSFER_QUEUES]

__all__ = [
    "CLAN_CHOICES",
    "CLAN_TRANSFER_QUEUES",
    "CLAN_TRANSFER_STATE_FILE",
    "CLAN_TRANSFER_THREADS",
    "CWL_SEASON_TRANSFER_CLANS",
    "REQUEST_TTL_HOURS",
]
