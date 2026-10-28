"""Command capabilities for the agent."""

from .registry import CommandAdapter, CommandCapability, build_command_capabilities

__all__ = ["CommandAdapter", "CommandCapability", "build_command_capabilities"]
