"""Typed reads selected by the model."""
from .war_tools import TOOLS as WAR_TOOLS

from elbow_helper.agent.reads.identity_reads import TOOLS as IDENTITY_TOOLS

from elbow_helper.agent.reads.workflow_reads import TOOLS as WORKFLOW_TOOLS

from .reference import TOOLS as REFERENCE_TOOLS
from .ranked_tools import TOOLS as RANKED_TOOLS
from .access import TOOLS as ACCESS_TOOLS
from .donations import TOOLS as DONATION_TOOLS
from .bot_code import TOOLS as BOT_CODE_TOOLS

TOOLS = (*WAR_TOOLS, *IDENTITY_TOOLS, *WORKFLOW_TOOLS, *REFERENCE_TOOLS, *RANKED_TOOLS, *ACCESS_TOOLS,
         *DONATION_TOOLS, *BOT_CODE_TOOLS)
