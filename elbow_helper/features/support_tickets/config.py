from __future__ import annotations

SCAN_RETRY_COUNT = 5
SCAN_RETRY_DELAY_SECONDS = 2.0
STARTUP_SCAN_DELAY_SECONDS = 1.5

TICKET_TYPES = {
    "question": {
        "trigger": "will be with you soon to help you with your question",
        "emoji": "❓",
        "short": "question",
    },
    "alliance_member": {
        "trigger": "will help you get sorted with getting set up here",
        "emoji": "🏛",
        "short": "alliance",
    },
    "join_leadership": {
        "trigger": "thanks for interest in joining our leadership",
        "emoji": "👑",
        "short": "applicant",
    },
    "intern": {
        "trigger": "will be with you soon to assist you with the internship process",
        "emoji": "🤝",
        "short": "intern",
    },
    "clan_promo": {
        "trigger": "will soon check with you regarding your clan promotion",
        "emoji": "🏡",
        "short": "promotion",
    },
    "elder_promo": {
        "trigger": "will soon check with you regarding your elder promotion",
        "emoji": "🪬",
        "short": "promotion",
    },
    "helper_cwl": {
        "trigger": "thanks for reaching out about cwl assistance",
        "emoji": "🏅",
        "short": "helper",
    },
}
