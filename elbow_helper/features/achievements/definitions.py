"""Achievement definitions, reward values, and presentation text."""

from __future__ import annotations

# Achievement notification phrases
ACHIEVEMENT_PHRASES = [
    "earned",
    "just earned",
    "has earned",
    "has just earned",
    "unlocked",
    "has unlocked",
]

COIN_REWARDS = {
    "fresh_recruit": 10,
    "storyteller": 10,
    "emoji_enthusiast": 10,
    "mic_check": 10,
    "party_animal": 10,
    "clan_hopper": 10,
    "social_butterfly": 10,
    "ping_collector": 10,
    "meme_dealer": 10,
    "one_of_us": 15,
    "chatterbox": 15,
    "react_lord": 15,
    "promoted": 15,
    "hibernation_survivor": 15,
    "random_crit": 15,
    "daily_streaker": 15,
    "channel_explorer": 15,
    "keyboard_warrior": 20,
    "silent_lurker": 20,
    "weekly_warrior": 20,
    "early_bird": 20,
    "night_owl": 20,
    "marathoner": 25,
    "monthly_master": 25,
    "veteran": 25,
}

ALL_ACHIEVEMENTS = [
    ("fresh_recruit", "Fresh Recruit", "Earn the Trial role.", 1, "🆕", False),
    ("one_of_us", "One of Us", "Reach 1 month in the server", 30, "📈", False),
    ("veteran", "Veteran", "Reach one year in the server.", 365, "🎊", False),
    ("chatterbox", "Chatterbox", "Send 100 messages", 100, "🗣️", False),
    ("keyboard_warrior", "Keyboard Warrior", "Send 1,000 messages", 1000, "⚔️", False),
    ("storyteller", "Storyteller", "Send a message over 500 characters", 1, "📖", False),
    ("emoji_enthusiast", "Emoji Enthusiast", "Send 50 emojis in chat.", 50, "😄", False),
    ("react_lord", "React Lord", "React 100 times to messages", 100, "👍", False),
    ("mic_check", "Mic Check", "Join a voice channel for the first time", 1, "🎤", False),
    ("silent_lurker", "Silent Lurker", "Spend 10 total hours in voice channels without unmuting.", 10, "🤫", False),
    ("party_animal", "Party Animal", "Join a voice channel with at least five members.", 1, "🎉", False),
    ("marathoner", "Marathoner", "Spend 24 total hours in voice channels.", 24, "🏃", False),
    ("promoted", "Promoted", "Earn a higher clan role.", 1, "⭐", False),
    ("clan_hopper", "Clan Hopper", "Transfer to another clan", 1, "🔄", False),
    ("hibernation_survivor", "Hibernation Survivor", "Return from hibernation", 1, "🌱", False),
    ("social_butterfly", "Social Butterfly", "Be active in at least five different channels.", 5, "🦋", False),
    ("ping_collector", "Ping Collector", "Get pinged 25 times by other members", 25, "🔔", False),
    ("meme_dealer", "Meme Dealer", "Post in #🐈║memes 25 times", 25, "😂", False),
    ("random_crit", "Random Crit", "Find a hidden chat easter egg.", 1, "🎯", False),
    ("daily_streaker", "Daily Streaker", "Be active for 7 consecutive days", 7, "🔥", False),
    ("weekly_warrior", "Weekly Warrior", "Be active for 4 weeks in a row", 28, "⚔️", False),
    ("monthly_master", "Monthly Master", "Be active for 3 consecutive months", 90, "👑", False),
    ("early_bird", "Early Bird", "Be active from 4 AM to 10 AM for 10 days", 10, "🌅", False),
    ("night_owl", "Night Owl", "Be active from 10 PM to 4 AM for 10 days", 10, "🦉", False),
    ("channel_explorer", "Channel Explorer", "Be active in at least eight different channels.", 8, "🗺️", False),
]
