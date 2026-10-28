from __future__ import annotations

from pathlib import Path

CREATED_TICKETS_FILE = Path("data/support/created_tickets.json")

# SQLite inventory relative to the application's data root. Database owners and
# the nightly backup job share these path definitions.
ACCOUNT_LINKS_DATABASE = Path("clan_links/links.sqlite3")
ACHIEVEMENTS_DATABASE = Path("achievements/achievements.db")
AGENT_CONVERSATIONS_DATABASE = Path("agent/agent.sqlite3")
AGENT_TRANSCRIPTS_DATABASE = Path("agent/transcripts.sqlite3")
AGENT_RESEARCH_DATABASE = Path("agent/research_jobs.sqlite3")
AGENT_ACTIONS_DATABASE = Path("agent/actions.sqlite3")
CLAN_HEALTH_DATABASE = Path("clan_health/clan_health.db")
RANKED_DATABASE = Path("ranked_stats/ranked.sqlite3")
RECORDS_DATABASE = Path("records/records.sqlite3")
ROSTERS_DATABASE = Path("rosters/rosters.sqlite3")
FAMILY_DATABASE = Path("family/family.sqlite3")

SQLITE_DATABASES = (
    ACCOUNT_LINKS_DATABASE,
    ACHIEVEMENTS_DATABASE,
    AGENT_CONVERSATIONS_DATABASE,
    AGENT_TRANSCRIPTS_DATABASE,
    AGENT_RESEARCH_DATABASE,
    AGENT_ACTIONS_DATABASE,
    CLAN_HEALTH_DATABASE,
    RANKED_DATABASE,
    RECORDS_DATABASE,
    ROSTERS_DATABASE,
    FAMILY_DATABASE,
)
