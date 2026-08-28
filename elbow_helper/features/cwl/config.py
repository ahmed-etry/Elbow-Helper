"""CWL-local constants, routing maps, and command choice data."""

from __future__ import annotations

from pathlib import Path

from discord import app_commands

from elbow_helper.configuration.clans import CLAN_CWL_HELPER_ROLE_IDS
from elbow_helper.configuration.clans import CLAN_CWL_ROLE_IDS
from elbow_helper.configuration.clans import CLAN_MEMBER_ROLE_IDS
from elbow_helper.configuration.clans import CLAN_NAMES
from elbow_helper.configuration.clans import CLAN_ORDER
from elbow_helper.configuration.clans import CLAN_TAGS

THREAD_STATE_FILE = Path("data/cwl/cwl_threads.json")
SCHEDULER_STATE_FILE = Path("data/cwl/cwl_scheduler_state.json")
TRANSFER_STATE_FILE = Path("data/cwl/cwl_transfer_state.json")
DASHBOARD_STATE_FILE = Path("data/cwl/cwl_dashboard_state.json")
ROUTER_STATE_FILE = Path("data/cwl/cwl_router_state.json")
BONUS_CONFIG_FILE = Path("data/cwl/cwl_bonus_config.json")
CWL_EXPORT_DIR = Path("data/.exports")
BONUS_DASHBOARD_STATE_FILE = Path("data/cwl/cwl_bonus_dashboard_state.json")
BONUS_AUTOMATION_START_MONTH_KEY = (2026 * 12) + 5

# Temporary feature switch for the bonus workflow.
CWL_BONUS_ECONOMY_ENABLED = False

CWL_HQ_CHANNEL_ID = 1191331025666723941

STICKY_REFRESH_HOURS = 1
STICKY_HTTP_RETRY_DELAYS_SECONDS = (1.0, 2.0, 4.0)
STICKY_HTTP_MAX_RATE_LIMIT_RETRY_SECONDS = 15.0
CWL_EXPORT_RETENTION_DAYS = 30
HERO_SUM_CACHE_SECONDS = 6 * 60 * 60
LEAGUE_NAME_CACHE_SECONDS = 6 * 60 * 60
MANUAL_DASHBOARD_REFRESH_COOLDOWN_SECONDS = 90
DASHBOARD_REFRESH_RETRIES = 3
DASHBOARD_REFRESH_BACKOFF_SECONDS = 1.5
DASHBOARD_WARNING_COOLDOWN_SECONDS = 300.0
TRANSFER_REMINDER_RETENTION_HOURS = 12

CWL_CLAN_CODES = tuple(code for code in CLAN_ORDER if code in CLAN_CWL_ROLE_IDS)
CWL_CLAN_NAMES = {code: CLAN_NAMES[code] for code in CWL_CLAN_CODES}
CWL_CLAN_TAGS = {code: CLAN_TAGS[code] for code in CWL_CLAN_CODES}
CLAN_NAME_TO_TAG = {CLAN_NAMES[code]: CLAN_TAGS[code] for code in CWL_CLAN_CODES}

THREAD_CLAN_CONFIGS = {
    CLAN_NAMES[code]: {
        "thread_id": None,
        "clan_name": CLAN_NAMES[code],
        "cc_reminder_role": CLAN_CWL_HELPER_ROLE_IDS.get(code),
    }
    for code in CWL_CLAN_CODES
}

THREAD_CLAN_CHOICES = [
    app_commands.Choice(name=CLAN_NAMES[code], value=CLAN_NAMES[code])
    for code in CWL_CLAN_CODES
]

CLAN_CHOICES = [app_commands.Choice(name=code, value=code) for code in CWL_CLAN_CODES]
BONUS_CLAN_CHOICES = [app_commands.Choice(name="ALL", value="ALL"), *CLAN_CHOICES]
BRIEF_MODES = [
    app_commands.Choice(name="Highly Motivated", value="highly_motivated"),
    app_commands.Choice(name="Mainline Pushing", value="mainline_pushing"),
    app_commands.Choice(name="Mainline Maintain", value="mainline_maintain"),
]
ROSTER_DEADLINE_MODES = [
    app_commands.Choice(name="Single deadline for all", value="single"),
    app_commands.Choice(name="Main deadline + extra time for some clans", value="preferred_delayed"),
]

CLAN_LINKS = {code: f"http://cprk.us/c/{CLAN_TAGS[code].lstrip('#')}" for code in CWL_CLAN_CODES}
WAR_SPECIALIST_ROLE_MENTION = f"<@&{CLAN_MEMBER_ROLE_IDS['BEW']}>"

CWL_THREADS: dict[str, int] = {
    "BEH": 1412086029153734728,
    "BE4": 1412086493635154051,
    "BES": 1412086880585126071,
    "BE1": 1412087195854180612,
    "BEM": 1412087785409745087,
    "BEP": 1412088283755970723,
    "BEC": 1412088541801877606,
    "BEE": 1412088806450135142,
}

BONUS_THREADS: dict[str, int] = {
    "BEH": 1323748083179196529,
    "BE4": 1323743840678576253,
    "BES": 1367563534266925096,
    "BE1": 1323752611370897418,
    "BEM": 1327718575435288656,
    "BEC": 1327702375930138746,
    "BEP": 1327713597375254594,
    "BEE": 1356459335110365335,
}

DASHBOARD_THREADS: dict[str, int] = {
    "BEH": 1470082353522151598,
    "BE4": 1470082690895188114,
    "BES": 1470082807299706901,
    "BE1": 1470083220145045504,
    "BEM": 1470083323123601478,
    "BEC": 1470083471350173770,
    "BEP": 1470083579378798603,
    "BEE": 1470083710220239093,
}

CLAN_DELTA_STAR_MULTIPLIERS = {
    0: 0.00,
    1: 0.80,
    2: 0.95,
    3: 1.00,
}
