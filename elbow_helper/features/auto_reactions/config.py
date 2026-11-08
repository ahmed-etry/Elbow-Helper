"""Auto-tools config and shared channel wiring."""

from __future__ import annotations

from elbow_helper.configuration.channels import ANNOUNCEMENT
from elbow_helper.configuration.channels import CLAN_TRANSFERS
from elbow_helper.configuration.channels import CWL_SIGNUP
from elbow_helper.configuration.channels import LEAD_NEWS
from elbow_helper.configuration.channels import PUBLIC_NEWS

REACT_ALLOWED_CHANNEL_IDS = frozenset(
    {
        ANNOUNCEMENT,
        PUBLIC_NEWS,
        CWL_SIGNUP,
        CLAN_TRANSFERS,
        LEAD_NEWS,
    }
)

AUTO_REACTION_SILENCE_WINDOW_SECONDS = 60.0
AUTO_REACTION_MAX_EMOJIS = 10

AUTO_REACTION_EXCLUDED_EMOJIS = frozenset(
    {
        "0️⃣",
        "1️⃣",
        "2️⃣",
        "3️⃣",
        "4️⃣",
        "5️⃣",
        "6️⃣",
        "7️⃣",
        "8️⃣",
        "9️⃣",
        "🔟",
        "⬆️",
        "⬇️",
        "⬅️",
        "➡️",
        "↗️",
        "↘️",
        "↙️",
        "↖️",
        "▪️",
        "▫️",
        "◾",
        "◽",
        "🔹",
        "🔸",
        "🔺",
        "🔻",
        "🟦",
        "🟥",
        "🟩",
        "🟨",
        "⬛",
        "⬜",
        "🕐",
        "🕑",
        "🕒",
        "🕓",
        "🕔",
        "🕕",
        "🕖",
        "🕗",
        "🕘",
        "🕙",
        "🕚",
        "🕛",
        "⏰",
        "⌛",
        "✅",
        "✔️",
        "☑️",
        "❌",
        "✖️",
        "⚠️",
        "ℹ️",
        "👩",
        "🚧",
        "💬",
        "🔁",
        "🔃",
        "🔗",
        "🏡",
        "🏠",
        "📋",
        "⛔",
        "🌚",
        "🧭",
        "🍩",
        "😺",
        "🐝",
        "🌴",
    }
)
