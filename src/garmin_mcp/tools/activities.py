"""Activity-related MCP tools."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from garminconnect import Garmin
from mcp.server.fastmcp import FastMCP

from garmin_mcp.cache import TTL_ACTIVITY_FINAL, TTL_ACTIVITY_LIST, cached
from garmin_mcp.tools._helpers import audited, coerce_date, safe_call

ClientFactory = Callable[[], Garmin]


def register(mcp: FastMCP, client_factory: ClientFactory) -> None:
    @mcp.tool()
    @audited
    @safe_call
    @cached(ttl=TTL_ACTIVITY_LIST)
    def list_recent_activities(limit: int = 20, start: int = 0) -> list[dict[str, Any]]:
        """List the user's most recent activities, newest first.

        Args:
            limit: Number of activities to return (default 20).
            start: Pagination offset (default 0).
        """
        result = client_factory().get_activities(start, limit)
        # The SDK returns either list or dict depending on endpoint shape; normalise.
        if isinstance(result, dict):
            return result.get("activityList", []) or []
        return result or []

    @mcp.tool()
    @audited
    @safe_call
    @cached(ttl=TTL_ACTIVITY_FINAL)
    def get_activity(activity_id: int) -> dict[str, Any]:
        """Fetch full details for a single activity by ID."""
        return client_factory().get_activity(str(activity_id))

    @mcp.tool()
    @audited
    @safe_call
    @cached(ttl=TTL_ACTIVITY_FINAL)
    def get_activity_splits(activity_id: int) -> dict[str, Any]:
        """Per-split (lap) breakdown for an activity."""
        return client_factory().get_activity_splits(str(activity_id))

    @mcp.tool()
    @audited
    @safe_call
    @cached(ttl=TTL_ACTIVITY_FINAL)
    def get_activity_hr_zones(activity_id: int) -> list[dict[str, Any]]:
        """Time-in-HR-zone breakdown for an activity (one entry per zone)."""
        return client_factory().get_activity_hr_in_timezones(str(activity_id)) or []

    @mcp.tool()
    @audited
    @safe_call
    @cached(ttl=TTL_ACTIVITY_LIST)
    def search_activities_by_type(
        type: str,
        start_date: str,
        end_date: str,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Search activities of a given type within a date range.

        Args:
            type: Garmin activity type (e.g. "running", "cycling", "swimming").
            start_date: Inclusive ISO start date (YYYY-MM-DD).
            end_date: Inclusive ISO end date (YYYY-MM-DD).
            limit: Maximum activities to return (default 50).
        """
        s = coerce_date(start_date)
        e = coerce_date(end_date)
        results = client_factory().get_activities_by_date(s, e, activitytype=type)
        return (results or [])[:limit]
