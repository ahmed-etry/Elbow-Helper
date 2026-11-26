from __future__ import annotations


from elbow_helper.configuration.clans import CLAN_CWL_ROLE_IDS, CLAN_MEMBER_ROLE_IDS, CLAN_WAR_ROLE_IDS


TICKET_LOG_SCAN_BATCH_SIZE = 200
TICKET_LOG_SCAN_BATCH_DELAY_SECONDS = 0.2
TICKET_LOG_SCAN_RETRY_SECONDS = 2.0
TICKET_LOG_SCAN_MAX_RETRY_SECONDS = 10.0
MAX_OVERDUE_APPLICANTS_DISPLAY = 15


APPLICANT_LINGER_DAYS = 14

# The recruitment report's order, each with its application emoji; Referral stays last and has none.
PLATFORM_EMOJIS = (
    ("Reddit", "from_reddit"), ("CoC Discord", "from_coc_discord"),
    ("Clash Champs", "from_clash_champs"), ("Jo Nation", "from_jo_nation"), ("Referral", None),
)

VISIBLE_LINE_CHARS = 30
LEAVE_PHRASES = (
    "Chose to kick rocks", "Packed up and dipped", "Folded under pressure", "Couldn't hang",
    "Tapped out", "Fumbled the bag", "Ran it back to the lobby", "Caught a case of the quits",
    "Bounced", "Said less and left",
)

HOME_CLAN_CODES = ("BEH", "BE4", "BES", "BE1", "BEM", "BEC", "BEE", "BEP")
HOME_CLAN_ROLE_IDS = tuple(CLAN_MEMBER_ROLE_IDS[code] for code in HOME_CLAN_CODES)
WAR_ROLE_IDS = tuple(role_id for code, role_id in CLAN_WAR_ROLE_IDS.items() if code in HOME_CLAN_CODES)
CWL_ROLE_IDS = tuple(role_id for code, role_id in CLAN_CWL_ROLE_IDS.items() if code in HOME_CLAN_CODES)
