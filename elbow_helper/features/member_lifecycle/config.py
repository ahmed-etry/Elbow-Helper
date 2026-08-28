from __future__ import annotations

from pathlib import Path

from elbow_helper.configuration.clans import CLAN_CWL_ROLE_IDS, CLAN_MEMBER_ROLE_IDS, CLAN_NAMES, CLAN_WAR_ROLE_IDS

STATE_FILE = Path("data/snapshot_intel/snapshot_intel.json")


MAX_TICKET_LINKS_IN_LEAVE_EMBED = 3
TICKET_LOG_SCAN_BATCH_SIZE = 200
TICKET_LOG_SCAN_BATCH_DELAY_SECONDS = 0.2
TICKET_LOG_SCAN_RETRY_SECONDS = 2.0
TICKET_LOG_SCAN_MAX_RETRY_SECONDS = 10.0
MAX_OVERDUE_APPLICANTS_DISPLAY = 15


WEEKLY_REPORT_INTERVAL_DAYS = 7
APPLICANT_LINGER_DAYS = 14

INVITER_TO_PLATFORM = {
    "Ubasauce": "Reddit / In-game",
    "Wesker": "Jo Nation",
    "Ahmad": "Clash Champs",
    "Gustas": "CoC Discord",
}

HOME_CLAN_CODES = ("BEH", "BE4", "BES", "BE1", "BEM", "BEC", "BEE", "BEP")
HOME_CLAN_ROLE_IDS = {CLAN_NAMES[code]: CLAN_MEMBER_ROLE_IDS[code] for code in HOME_CLAN_CODES}
HIBERNATION_CLAN_ROLES = {CLAN_MEMBER_ROLE_IDS[code]: CLAN_NAMES[code] for code in HOME_CLAN_CODES}
WAR_ROLE_IDS = {
    f"War in {CLAN_NAMES[code]}": role_id
    for code, role_id in CLAN_WAR_ROLE_IDS.items()
    if code in HOME_CLAN_CODES
}
CWL_ROLE_IDS = {
    f"CWL in {CLAN_NAMES[code]}": role_id
    for code, role_id in CLAN_CWL_ROLE_IDS.items()
    if code in HOME_CLAN_CODES
}
