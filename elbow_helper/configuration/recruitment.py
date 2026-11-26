"""Recruitment sources shown as a joining member's Platform."""

INVITER_TO_PLATFORM = {
    "Ubasauce": "Reddit",
    "Wesker": "Jo Nation",
    "Ahmad": "Clash Champs",
    "Gustas": "CoC Discord",
}
# A join through anyone else's invite, and an invite that could not be read.
REFERRAL_PLATFORM = "Referral"
PLATFORMS = frozenset({*INVITER_TO_PLATFORM.values(), REFERRAL_PLATFORM})
# Names that earlier joins were recorded under.
_FORMER_PLATFORMS = {"Reddit / In-game": "Reddit", "Unknown": REFERRAL_PLATFORM}


def platform_name(recorded):
    """The platform a recorded join counts under."""
    return _FORMER_PLATFORMS.get(recorded, recorded)
