"""MCP tool registration."""

from __future__ import annotations

from collections.abc import Callable

from garminconnect import Garmin
from mcp.server.fastmcp import FastMCP

from garmin_mcp.tools import activities, aggregate, training, wellness

ClientFactory = Callable[[], Garmin]


def register_all(mcp: FastMCP, client_factory: ClientFactory) -> None:
    """Register every tool with the FastMCP instance."""
    activities.register(mcp, client_factory)
    wellness.register(mcp, client_factory)
    training.register(mcp, client_factory)
    aggregate.register(mcp, client_factory)
