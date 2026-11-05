"""Typed reads selected by the model."""
from .war_tools import TOOLS as WAR_TOOLS

from ..families.identity_reads import TOOLS as IDENTITY_TOOLS

from ..families.workflow_reads import TOOLS as WORKFLOW_TOOLS

from .reference import TOOLS as REFERENCE_TOOLS
from .ranked_tools import TOOLS as RANKED_TOOLS
from .access import TOOLS as ACCESS_TOOLS

TOOLS = (*WAR_TOOLS, *IDENTITY_TOOLS, *WORKFLOW_TOOLS, *REFERENCE_TOOLS, *RANKED_TOOLS, *ACCESS_TOOLS)
