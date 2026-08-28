"""Statement templates and routing constants for /warstatement commands."""

from __future__ import annotations

from discord import app_commands

from elbow_helper.configuration.channels import CLAN_CHAT_CHANNELS
from elbow_helper.configuration.channels import CLAN_WAR_CHANNELS
from elbow_helper.configuration.clans import CLAN_ORDER

# Clan routing for war statements: post channel and clan-war channel by clan code.
def _build_clan_channel_map() -> dict[str, dict[str, int]]:
    return {
        code: {
            "post_channel": CLAN_CHAT_CHANNELS[code],
            "clan_war_channel": CLAN_WAR_CHANNELS[code],
        }
        for code in CLAN_ORDER
        if code in CLAN_CHAT_CHANNELS and code in CLAN_WAR_CHANNELS
    }


CLAN_CHANNEL_MAP = _build_clan_channel_map()
CLAN_CHOICES = [app_commands.Choice(name=clan, value=clan) for clan in CLAN_CHANNEL_MAP]

WAR_STATEMENTS = {
    "attacked_first_claim": {
        "template": (
            "{attacker}, you attacked {victim}'s first claim in war. First claims are protected; "
            "only second claims are open to other players.\n"
            "This rule is explained in {clan_war_channel} and gives everyone a fair chance to attack "
            "the base they chose.\n"
            "Why did you attack {victim}'s first claim without asking them?"
        ),
    },
    "breaking_rules": {
        "template": "{users} you were removed from the war roster because you didn't follow the war rules or tell us what happened.",
    },
    "one_attack_missed": {
        "template": "{users} you used only one war attack. Please use both, even when every base has been cleared.\nThe extra attack earns more ores and shows that you're active. \nWhat stopped you from using your second attack?",
    },
    "war_filler": {
        "template": "{users} you were recently online and had war availability on, so you were added for war.\nYou won't be taken in the next war if you do no attacks, but you can sign up in {clan_war_channel} if you want to join wars",
    },
    "missed_attacks": {
        "template": "{users} you were removed from the war roster after missing both attacks without letting us know why.\nYou can sign up again in {clan_war_channel} when you're able to use both attacks.",
    },
}
