"""Complete role/account snapshots and their deterministic workbook output."""

from __future__ import annotations

from dataclasses import dataclass, field
from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import json
from typing import Any

from elbow_helper.configuration.clans import CLANS
from elbow_helper.domain.player_tags import normalize_player_tag


@dataclass(frozen=True, slots=True)
class RoleAccountReport:
    report_id: str
    created_at: str
    roles: tuple[dict[str, Any], ...]
    members: tuple[dict[str, Any], ...]
    parent_report_id: str | None = None
    refresh_attempted_player_tags: tuple[str, ...] = ()
    retained_bytes: int = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        account_tags = {
            account["player_tag"]
            for member in self.members
            for account in member["accounts"]
        }
        if (
            self.parent_report_id is not None
            and (
                not self.parent_report_id
                or len(self.parent_report_id) > 32
                or self.parent_report_id == self.report_id
            )
            or len(set(self.refresh_attempted_player_tags))
            != len(self.refresh_attempted_player_tags)
            or any(
                normalize_player_tag(value) != value
                for value in self.refresh_attempted_player_tags
            )
            or not set(self.refresh_attempted_player_tags) <= account_tags
        ):
            raise ValueError("Invalid role account report refresh lineage")
        payload = {"report_id": self.report_id, "created_at": self.created_at,
                   "roles": self.roles, "members": self.members,
                   "parent_report_id": self.parent_report_id,
                   "refresh_attempted_player_tags": self.refresh_attempted_player_tags}
        object.__setattr__(self, "retained_bytes", len(json.dumps(payload, ensure_ascii=False).encode("utf-8")))

    def manifest(self) -> dict[str, Any]:
        accounts = [account for member in self.members for account in member["accounts"]]
        return {"report_id": self.report_id, "created_at": self.created_at,
                "kind": "role_accounts", "roles": self.roles,
                "parent_report_id": self.parent_report_id,
                "refresh_attempted_player_tags": list(
                    self.refresh_attempted_player_tags
                ),
                "total_members": len(self.members), "total_accounts": len(accounts),
                "members_without_links": sum(not member["accounts"] for member in self.members),
                "unverified_accounts": sum(
                    account["location_status"] != "refreshed" for account in accounts
                )}

    def fingerprint(self) -> str:
        payload = {
            "report_id": self.report_id, "created_at": self.created_at,
            "roles": self.roles, "members": self.members,
            "parent_report_id": self.parent_report_id,
            "refresh_attempted_player_tags": self.refresh_attempted_player_tags,
        }
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
        return sha256(encoded).hexdigest()

    def select(self, selection: str = "all", clan_code: str | None = None, member_id: int | None = None) -> list[dict[str, Any]]:
        clan = CLANS.get(clan_code or "")
        if selection == "outside_clan" and clan is None:
            raise ValueError("A known clan_code is required for outside_clan.")
        selected = []
        for member in self.members:
            if member_id is not None and member["member_id"] != member_id:
                continue
            accounts = member["accounts"]
            if selection == "no_links" and accounts:
                continue
            if selection == "unverified" and not any(account["location_status"] != "refreshed" for account in accounts):
                continue
            if selection == "outside_clan" and (
                not accounts or any(account["location_status"] != "refreshed" or account["clan_tag"] == clan.tag for account in accounts)
            ):
                continue
            selected.append(member)
        return selected

    def page(self, *, selection: str = "all", clan_code: str | None = None, member_id: int | None = None, offset: int = 0, limit: int = 10) -> dict[str, Any]:
        selected = self.select(selection, clan_code, member_id)
        accounts = [account for member in self.members for account in member["accounts"]]
        return {
            "report_id": self.report_id,
            "created_at": self.created_at,
            "roles": self.roles,
            "total_members": len(self.members),
            "total_accounts": len(accounts),
            "members_without_links": sum(not member["accounts"] for member in self.members),
            "refreshed_accounts": sum(account["location_status"] == "refreshed" for account in accounts),
            "unverified_accounts": sum(account["location_status"] != "refreshed" for account in accounts),
            "selection": selection,
            "matching_members": len(selected),
            "members": selected[offset:offset + limit],
            "next_offset": offset + limit if offset + limit < len(selected) else None,
            "note": "The report retains all matching members. This response is one page. "
                    "Use read_role_account_report for other pages or filters. "
                    "No links means membership is unverified, not absent.",
        }


def revise_role_account_report(
    source: RoleAccountReport,
    refreshed_accounts: tuple[dict[str, Any], ...],
    *,
    report_id: str,
    created_at: str | None = None,
) -> RoleAccountReport:
    """Create one immutable location revision while preserving report identity facts."""

    refreshed_by_tag = {row["player_tag"]: deepcopy(row) for row in refreshed_accounts}
    source_accounts = {
        account["player_tag"]: account
        for member in source.members for account in member["accounts"]
    }
    if (
        len(refreshed_by_tag) != len(refreshed_accounts)
        or not refreshed_by_tag.keys() <= source_accounts.keys()
        or any(
            row["primary"] != source_accounts[tag]["primary"]
            for tag, row in refreshed_by_tag.items()
        )
    ):
        raise ValueError("Role refresh results do not match the source report")
    members = deepcopy(source.members)
    for member in members:
        member["accounts"] = [
            refreshed_by_tag.get(account["player_tag"], account)
            for account in member["accounts"]
        ]
    attempted = tuple(dict.fromkeys((
        *source.refresh_attempted_player_tags, *refreshed_by_tag,
    )))
    return RoleAccountReport(
        report_id=report_id,
        created_at=created_at or datetime.now(timezone.utc).isoformat(),
        roles=source.roles, members=tuple(members),
        parent_report_id=source.report_id,
        refresh_attempted_player_tags=attempted,
    )
