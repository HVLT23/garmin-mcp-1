"""Wellness MCP tools (sleep, HRV, body battery, stress, steps, daily summary)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from garminconnect import Garmin
from mcp.server.fastmcp import FastMCP

from garmin_mcp.cache import TTL_WELLNESS, cached
from garmin_mcp.tools._helpers import audited, coerce_date, safe_call

ClientFactory = Callable[[], Garmin]


def register(mcp: FastMCP, client_factory: ClientFactory) -> None:
    @mcp.tool()
    @audited
    @safe_call
    @cached(ttl=TTL_WELLNESS)
    def get_sleep(date: str | None = None) -> dict[str, Any]:
        """Sleep details (stages, duration, score) for a given ISO date.
        Defaults to today.
        """
        return client_factory().get_sleep_data(coerce_date(date))

    @mcp.tool()
    @audited
    @safe_call
    @cached(ttl=TTL_WELLNESS)
    def get_hrv(date: str | None = None) -> dict[str, Any]:
        """Heart-rate variability summary for a given ISO date. Defaults to today."""
        result = client_factory().get_hrv_data(coerce_date(date))
        return result or {}

    @mcp.tool()
    @audited
    @safe_call
    @cached(ttl=TTL_WELLNESS)
    def get_body_battery(start_date: str, end_date: str | None = None) -> list[dict[str, Any]]:
        """Body battery readings across a date range (inclusive).

        Args:
            start_date: ISO start date.
            end_date: ISO end date (defaults to start_date).
        """
        s = coerce_date(start_date)
        e = coerce_date(end_date) if end_date else s
        return client_factory().get_body_battery(s, e) or []

    @mcp.tool()
    @audited
    @safe_call
    @cached(ttl=TTL_WELLNESS)
    def get_stress(date: str | None = None) -> dict[str, Any]:
        """Stress timeline for a given ISO date. Defaults to today."""
        return client_factory().get_stress_data(coerce_date(date))

    @mcp.tool()
    @audited
    @safe_call
    @cached(ttl=TTL_WELLNESS)
    def get_steps(date: str | None = None) -> list[dict[str, Any]]:
        """Step counts (15-min buckets) for a given ISO date. Defaults to today."""
        return client_factory().get_steps_data(coerce_date(date)) or []

    @mcp.tool()
    @audited
    @safe_call
    @cached(ttl=TTL_WELLNESS)
    def get_daily_summary(date: str | None = None) -> dict[str, Any]:
        """Daily user summary (steps, calories, intensity minutes, RHR, etc.).

        Defaults to today.
        """
        return client_factory().get_user_summary(coerce_date(date))
