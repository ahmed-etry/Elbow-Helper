"""Deterministic comparisons for complete retained role/account reports."""

from __future__ import annotations

from typing import Any

from elbow_helper.domain.player_tags import normalize_player_tag

from .roles import RoleAccountReport


ACCOUNT_LINK_FIELDS = ("primary",)
ACCOUNT_PROFILE_FIELDS = ("player_name",)
ACCOUNT_LOCATION_FIELDS = (
    "clan_tag", "clan_code", "clan_name", "clan_role", "in_clan",
)
ACCOUNT_EVIDENCE_FIELDS = ("location_status",)
IGNORED_ACCOUNT_FIELDS = ("checked_at",)


def compare_role_account_reports(
    before: RoleAccountReport,
    after: RoleAccountReport,
    *,
    offset: int = 0,
    limit: int = 25,
) -> dict[str, Any]:
    """Compare exact retained snapshots without refreshing or inferring causes."""

    if offset < 0 or not 1 <= limit <= 25:
        raise ValueError("Invalid role account comparison page")
    before_roles = _roles(before)
    after_roles = _roles(after)
    before_members, before_accounts = _members_and_accounts(before)
    after_members, after_accounts = _members_and_accounts(after)

    changes: list[dict[str, Any]] = []
    role_counts = _empty_counts()
    member_counts = _empty_counts()
    account_counts = _empty_counts()

    for role_id in sorted(before_roles.keys() | after_roles.keys()):
        old, new = before_roles.get(role_id), after_roles.get(role_id)
        changed_fields = (
            ["name"] if old is not None and new is not None
            and old["name"] != new["name"] else []
        )
        change = _change_kind(old, new, changed_fields)
        role_counts[change] += 1
        if change != "unchanged":
            changes.append({
                "entity": "role", "role_id": role_id, "change": change,
                "changed_fields": changed_fields, "before": old, "after": new,
            })

    for member_id in sorted(before_members.keys() | after_members.keys()):
        old, new = before_members.get(member_id), after_members.get(member_id)
        changed_fields = []
        if old is not None and new is not None:
            changed_fields = [
                field for field in ("member", "matched_roles", "account_tags")
                if old[field] != new[field]
            ]
        change = _change_kind(old, new, changed_fields)
        member_counts[change] += 1
        if change != "unchanged":
            changes.append({
                "entity": "member", "member_id": member_id,
                "change": change, "changed_fields": changed_fields,
                "before": old, "after": new,
            })

    for tag in sorted(before_accounts.keys() | after_accounts.keys()):
        old, new = before_accounts.get(tag), after_accounts.get(tag)
        changed_fields = []
        classifications = []
        if old is not None and new is not None:
            if old["member_id"] != new["member_id"]:
                changed_fields.append("member_id")
                classifications.append("ownership_changed")
            for label, fields in (
                ("link_metadata_changed", ACCOUNT_LINK_FIELDS),
                ("account_profile_changed", ACCOUNT_PROFILE_FIELDS),
                ("location_changed", ACCOUNT_LOCATION_FIELDS),
                ("evidence_status_changed", ACCOUNT_EVIDENCE_FIELDS),
            ):
                names = [field for field in fields if old[field] != new[field]]
                changed_fields.extend(names)
                if names:
                    classifications.append(label)
        change = _change_kind(old, new, changed_fields)
        if change == "added":
            classifications = ["link_added"]
        elif change == "removed":
            classifications = ["link_removed"]
        account_counts[change] += 1
        if change != "unchanged":
            changes.append({
                "entity": "account", "player_tag": tag, "change": change,
                "classifications": classifications,
                "changed_fields": changed_fields,
                "before": old, "after": new,
            })

    return {
        "before": before.manifest(), "after": after.manifest(),
        "role_counts": role_counts, "member_counts": member_counts,
        "account_counts": account_counts,
        "total_changes": len(changes),
        "changes": changes[offset:offset + limit],
        "next_offset": offset + limit if offset + limit < len(changes) else None,
        "compared_account_fields": {
            "ownership": ["member_id"],
            "link_metadata": list(ACCOUNT_LINK_FIELDS),
            "account_profile": list(ACCOUNT_PROFILE_FIELDS),
            "location": list(ACCOUNT_LOCATION_FIELDS),
            "evidence_status": list(ACCOUNT_EVIDENCE_FIELDS),
        },
        "ignored_account_fields": list(IGNORED_ACCOUNT_FIELDS),
        "complete_comparison": True,
        "cause_scope": (
            "Changes describe retained snapshots only. They do not establish why "
            "a role, link, owner or observed location changed."
        ),
    }


def _roles(report: RoleAccountReport) -> dict[int, dict[str, Any]]:
    roles: dict[int, dict[str, Any]] = {}
    for role in report.roles:
        if (
            not isinstance(role, dict) or set(role) != {"role_id", "name"}
            or type(role["role_id"]) is not int or role["role_id"] <= 0
            or not isinstance(role["name"], str) or not role["name"]
            or role["role_id"] in roles
        ):
            raise ValueError("Role report contains invalid or duplicate roles")
        roles[role["role_id"]] = {
            "role_id": role["role_id"], "name": role["name"],
        }
    return roles


def _members_and_accounts(
    report: RoleAccountReport,
) -> tuple[dict[int, dict[str, Any]], dict[str, dict[str, Any]]]:
    members: dict[int, dict[str, Any]] = {}
    accounts: dict[str, dict[str, Any]] = {}
    required_account_fields = {
        "player_tag", "player_name", "primary", "clan_tag", "clan_code",
        "clan_name", "clan_role", "in_clan", "location_status", "checked_at",
    }
    for member in report.members:
        if (
            not isinstance(member, dict)
            or set(member) != {"member_id", "member", "matched_roles", "accounts"}
            or type(member["member_id"]) is not int or member["member_id"] <= 0
            or not isinstance(member["member"], str) or not member["member"]
            or not isinstance(member["matched_roles"], list)
            or any(not isinstance(value, str) or not value for value in member["matched_roles"])
            or not isinstance(member["accounts"], list)
            or member["member_id"] in members
        ):
            raise ValueError("Role report contains invalid or duplicate members")
        tags = []
        for account in member["accounts"]:
            if (
                not isinstance(account, dict) or set(account) != required_account_fields
                or normalize_player_tag(account["player_tag"]) != account["player_tag"]
                or not isinstance(account["player_name"], str)
                or not account["player_name"]
                or type(account["primary"]) is not bool
                or account["location_status"] not in {
                    "unknown", "last_known", "refresh_failed", "refreshed",
                }
                or account["player_tag"] in accounts
            ):
                raise ValueError("Role report contains invalid or duplicate accounts")
            tag = account["player_tag"]
            tags.append(tag)
            accounts[tag] = {
                "member_id": member["member_id"],
                **{field: account[field] for field in (
                    *ACCOUNT_LINK_FIELDS, *ACCOUNT_PROFILE_FIELDS,
                    *ACCOUNT_LOCATION_FIELDS,
                    *ACCOUNT_EVIDENCE_FIELDS, *IGNORED_ACCOUNT_FIELDS,
                )},
            }
        members[member["member_id"]] = {
            "member_id": member["member_id"], "member": member["member"],
            "matched_roles": sorted(member["matched_roles"]),
            "account_tags": sorted(tags),
        }
    return members, accounts


def _empty_counts() -> dict[str, int]:
    return {"added": 0, "removed": 0, "changed": 0, "unchanged": 0}


def _change_kind(old: Any, new: Any, changed_fields: list[str]) -> str:
    return (
        "added" if old is None else "removed" if new is None
        else "changed" if changed_fields else "unchanged"
    )


__all__ = ["compare_role_account_reports"]
