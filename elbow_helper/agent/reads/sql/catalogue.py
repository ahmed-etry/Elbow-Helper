"""Collect the datasets explicitly declared by their features."""

from elbow_helper.agent.features.account_links.data import SOURCES as ACCOUNT_LINKS
from elbow_helper.agent.features.clan_health.data import SOURCES as CLAN_HEALTH
from elbow_helper.agent.features.rosters.data import SOURCES as ROSTERS
from elbow_helper.agent.features.achievements.data import SOURCES as ACHIEVEMENTS
from elbow_helper.agent.features.cwl.data import SOURCES as CWL
from elbow_helper.agent.features.event_stats.data import SOURCES as EVENTS
from elbow_helper.agent.features.role_connections.data import SOURCES as ROLE_CONNECTIONS
from elbow_helper.agent.features.wars.data import SOURCES as WARS
from elbow_helper.agent.reads.sql.family import SOURCES as FAMILY


SOURCES = (
    *ACCOUNT_LINKS, *CLAN_HEALTH, *ROSTERS, *ACHIEVEMENTS, *CWL, *EVENTS,
    *ROLE_CONNECTIONS, *WARS, *FAMILY,
)
