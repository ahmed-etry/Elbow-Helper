"""Collect the datasets explicitly declared by their features."""

from ..capabilities.account_links.data import SOURCES as ACCOUNT_LINKS
from ..capabilities.clan_health.data import SOURCES as CLAN_HEALTH
from ..capabilities.rosters.data import SOURCES as ROSTERS
from ..capabilities.records.data import SOURCES as RECORDS
from ..capabilities.achievements.data import SOURCES as ACHIEVEMENTS
from ..capabilities.cwl.data import SOURCES as CWL
from ..capabilities.events.data import SOURCES as EVENTS
from ..capabilities.role_connections.data import SOURCES as ROLE_CONNECTIONS
from ..capabilities.wars.data import SOURCES as WARS


SOURCES = (
    *ACCOUNT_LINKS, *CLAN_HEALTH, *ROSTERS, *RECORDS, *ACHIEVEMENTS, *CWL, *EVENTS,
    *ROLE_CONNECTIONS, *WARS,
)
