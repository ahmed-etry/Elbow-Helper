"""Completed-war attendance from Clan Health storage."""

from __future__ import annotations


class ClanHealthWarReads:
    def attendance(self, **selection):
        from ..attendance import attendance_snapshot
        return attendance_snapshot(self.path, **selection)
