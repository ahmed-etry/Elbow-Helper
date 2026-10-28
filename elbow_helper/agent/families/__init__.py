"""Typed reads selected by the model."""
from .war_reads import TOOLS as WAR_TOOLS

from .identity_reads import TOOLS as IDENTITY_TOOLS

from .workflow_reads import TOOLS as WORKFLOW_TOOLS

from .reference_reads import TOOLS as REFERENCE_TOOLS
from .ranked_reads import TOOLS as RANKED_TOOLS
from .access_reads import TOOLS as ACCESS_TOOLS

TOOLS = (*WAR_TOOLS, *IDENTITY_TOOLS, *WORKFLOW_TOOLS, *REFERENCE_TOOLS, *RANKED_TOOLS, *ACCESS_TOOLS)
