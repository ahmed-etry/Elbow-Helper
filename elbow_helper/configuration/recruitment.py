"""Recruitment sources shown as a joining member's Platform."""

INVITER_TO_PLATFORM = {
    "Ubasauce": "Reddit / In-game",
    "Wesker": "Jo Nation",
    "Ahmad": "Clash Champs",
    "Gustas": "CoC Discord",
}
# A join through anyone else's invite, and an invite that could not be read.
REFERRAL_PLATFORM = "Referral"
UNKNOWN_PLATFORM = "Unknown"
PLATFORMS = frozenset({*INVITER_TO_PLATFORM.values(), REFERRAL_PLATFORM, UNKNOWN_PLATFORM})
