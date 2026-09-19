"""Clan transfers package entrypoint."""

from .cog import ClanTransfers
from .cog import setup
from .queries import ClanTransferQueries

__all__ = ["ClanTransferQueries", "ClanTransfers", "setup"]
