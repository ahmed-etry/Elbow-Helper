"""External AI integration contracts."""

from .agent import AgentModel
from .agent import AgentSession
from .agent import AgentStep
from .agent import AgentToolCall
from .agent import AgentToolDefinition
from .agent import AgentToolResult
from .agent import AgentUsage
from .client import AIClient
from .client import DeepSeekTextClient
from .client import GenerationTier
from .client import TextGenerationError
from .client import TextGenerator

__all__ = [
    "AgentModel",
    "AgentSession",
    "AgentStep",
    "AgentToolCall",
    "AgentToolDefinition",
    "AgentToolResult",
    "AgentUsage",
    "AIClient",
    "DeepSeekTextClient",
    "GenerationTier",
    "TextGenerationError",
    "TextGenerator",
]
