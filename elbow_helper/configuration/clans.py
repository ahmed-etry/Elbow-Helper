"""Canonical clan metadata and per-clan role mappings.

Each clan entry defines its tag and role wiring. Nullable role fields are
intentional for utility clans with partial role models.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class ClanConfig:
    """Immutable clan definition used by routing and permission lookups."""

    code: str
    name: str
    emoji: str
    tag: str
    is_utility: bool
    member_role_id: Optional[int]
    war_role_id: Optional[int]
    cwl_role_id: Optional[int]
    leadership_role_id: Optional[int]
    cwl_helper_role_id: Optional[int]
    cwl_bench_role_id: Optional[int]


CLAN_ORDER = ("BEH", "BE4", "BES", "BE1", "BEM", "BEC", "BEE", "BEP", "BEW")

# Primary clan index keyed by short clan code.
CLANS: dict[str, ClanConfig] = {
    "BEH": ClanConfig(
        code="BEH",
        name="Hellbow",
        emoji="📛",
        tag="#2Y2PJCVGU",
        is_utility=False,
        member_role_id=1135601362495885323,
        war_role_id=1168973121861206016,
        cwl_role_id=1142803238907813899,
        leadership_role_id=1150484000629071943,
        cwl_helper_role_id=1251809746009456661,
        cwl_bench_role_id=1246866601169522758,
    ),
    "BE4": ClanConfig(
        code="BE4",
        name="Brown Elbow IV",
        emoji="🍩",
        tag="#2YLRRYLC8",
        is_utility=False,
        member_role_id=1135601445455007774,
        war_role_id=1164931657325883592,
        cwl_role_id=1142795583959089243,
        leadership_role_id=1168971049006792714,
        cwl_helper_role_id=1251809893690638396,
        cwl_bench_role_id=1251812287891767396,
    ),
    "BES": ClanConfig(
        code="BES",
        name="TruNorth Stronk",
        emoji="🧭",
        tag="#8UCU09Q9",
        is_utility=False,
        member_role_id=1367411076924309525,
        war_role_id=1371468255750782986,
        cwl_role_id=1356447585954566339,
        leadership_role_id=1367566694301696080,
        cwl_helper_role_id=1356447591952552077,
        cwl_bench_role_id=1356447329909215293,
    ),
    "BE1": ClanConfig(
        code="BE1",
        name="Brown Elbow Inc",
        emoji="🌚",
        tag="#29Y2QC99",
        is_utility=False,
        member_role_id=1135601516393267390,
        war_role_id=1168973634354810891,
        cwl_role_id=1142803352971923546,
        leadership_role_id=1168971172386443316,
        cwl_helper_role_id=1251810411314020423,
        cwl_bench_role_id=1251812532814086144,
    ),
    "BEM": ClanConfig(
        code="BEM",
        name="urmomfavclan",
        emoji="👩",
        tag="#2LLJQYUC2",
        is_utility=False,
        member_role_id=1327717972512477254,
        war_role_id=1360682799127990392,
        cwl_role_id=1290635745903513632,
        leadership_role_id=1360683048307396711,
        cwl_helper_role_id=1290637470093934644,
        cwl_bench_role_id=1290637120238518282,
    ),
    "BEC": ClanConfig(
        code="BEC",
        name="Brown Elbow Cat",
        emoji="😺",
        tag="#2RR0RQ20Y",
        is_utility=False,
        member_role_id=1222858721127043113,
        war_role_id=1227569083109605426,
        cwl_role_id=1224076742562549870,
        leadership_role_id=1247568375358558339,
        cwl_helper_role_id=1251810035554582578,
        cwl_bench_role_id=1251812433379594361,
    ),
    "BEE": ClanConfig(
        code="BEE",
        name="Brown Elbow Eye",
        emoji="🐝",
        tag="#2R2GP2JCR",
        is_utility=False,
        member_role_id=1356441252547264733,
        war_role_id=1356441263020445818,
        cwl_role_id=1356441270427324516,
        leadership_role_id=1356441282507051209,
        cwl_helper_role_id=1356441275309494464,
        cwl_bench_role_id=1356440427107979406,
    ),
    "BEP": ClanConfig(
        code="BEP",
        name="Plutopia",
        emoji="🌴",
        tag="#2GYYC8PQG",
        is_utility=True,
        member_role_id=1180437929798160435,
        war_role_id=None,
        cwl_role_id=1324005097352462336,
        leadership_role_id=None,
        cwl_helper_role_id=1278824489316122765,
        cwl_bench_role_id=1324004956340228258,
    ),
    "BEW": ClanConfig(
        code="BEW",
        name="Brown Elbow War",
        emoji="⚔️",
        tag="#2LQ9LC898",
        is_utility=True,
        member_role_id=1202327291477373028,
        war_role_id=1245647848792653885,
        cwl_role_id=None,
        leadership_role_id=None,
        cwl_helper_role_id=None,
        cwl_bench_role_id=None,
    ),
}

# Derived lookup indexes for call sites that need targeted views.
CLAN_TAGS = {code: clan.tag for code, clan in CLANS.items()}
CLAN_NAMES = {code: clan.name for code, clan in CLANS.items()}
CLAN_EMOJIS = {code: clan.emoji for code, clan in CLANS.items()}
CLAN_CODES_BY_NAME = {clan.name: code for code, clan in CLANS.items()}

CLAN_MEMBER_ROLE_IDS = {code: clan.member_role_id for code, clan in CLANS.items() if clan.member_role_id is not None}
CLAN_WAR_ROLE_IDS = {code: clan.war_role_id for code, clan in CLANS.items() if clan.war_role_id is not None}
CLAN_CWL_ROLE_IDS = {code: clan.cwl_role_id for code, clan in CLANS.items() if clan.cwl_role_id is not None}
CLAN_LEADERSHIP_ROLE_IDS = {
    code: clan.leadership_role_id for code, clan in CLANS.items() if clan.leadership_role_id is not None
}
CLAN_CWL_HELPER_ROLE_IDS = {
    code: clan.cwl_helper_role_id for code, clan in CLANS.items() if clan.cwl_helper_role_id is not None
}

# Clan info board channels
CLAN_INFO_BOARD_CHANNEL_IDS = {
    "BEH": 1394738186944512030,
    "BE4": 1394763346619928627,
    "BES": 1394767884710052053,
    "BE1": 1394773451096723546,
    "BEM": 1394781348979409016,
    "BEC": 1394783434832085023,
    "BEE": 1394790721281724548,
}

# Clan board metadata used by features that need per-clan board routing.
CLAN_INFO_BOARDS = {
    code: {
        "channel_id": channel_id,
        "link": f"http://cprk.us/c/{CLAN_TAGS[code].lstrip('#')}",
        "clan_role": CLAN_MEMBER_ROLE_IDS.get(code),
    }
    for code, channel_id in CLAN_INFO_BOARD_CHANNEL_IDS.items()
}
