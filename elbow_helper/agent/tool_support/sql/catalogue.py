"""Collect the datasets explicitly declared by their features."""

from ..adapters.account_links.data import SOURCES as ACCOUNT_LINKS
from ...capabilities.clan_health.data import SOURCES as CLAN_HEALTH
from ..adapters.rosters.data import SOURCES as ROSTERS
from ..adapters.achievements.data import SOURCES as ACHIEVEMENTS
from ..adapters.cwl.data import SOURCES as CWL
from ..adapters.events.data import SOURCES as EVENTS
from ..adapters.role_connections.data import SOURCES as ROLE_CONNECTIONS
from ..adapters.wars.data import SOURCES as WARS
from .family import SOURCES as FAMILY


SOURCES = (
    *ACCOUNT_LINKS, *CLAN_HEALTH, *ROSTERS, *ACHIEVEMENTS, *CWL, *EVENTS,
    *ROLE_CONNECTIONS, *WARS, *FAMILY,
)
