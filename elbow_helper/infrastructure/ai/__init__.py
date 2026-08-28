"""External AI integration contracts."""

from .client import OpenAITextClient
from .client import TextGenerationError
from .client import TextGenerator

__all__ = ["OpenAITextClient", "TextGenerationError", "TextGenerator"]
