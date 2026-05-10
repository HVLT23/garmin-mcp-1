"""Wellness MCP tools (sleep, HRV, body battery, stress, steps, daily summary)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from garminconnect import Garmin
from mcp.server.fastmcp import FastMCP

from garmin_mcp.cache import TTL_WELLNESS, cached
from garmin_mcp.tools._helpers import audited, coerce_date, safe_call
from garmin_mcp.tools._trimmers import trim_body_battery, trim_sleep, trim_stress

ClientFactory = Callable[[], Garmin]


def register(mcp: FastMCP, client_factory: ClientFactory) -> None:
    # Cached fetcher for the full upstream sleep payload. Caching happens
    # *here* (before trimming) so a verbose=True call after a verbose=False
    # call hits the cache rather than re-fetching from Garmin.
    @cached(ttl=TTL_WELLNESS)
    def _fetch_sleep(date: str) -> dict[str, Any]:
        return client_factory().get_sleep_data(date)

    @mcp.tool()
    @audited
    @safe_call
    def get_sleep(date: str | None = None, verbose: bool = False) -> dict[str, Any]:
        """Sleep details (stages, duration, score) for a given ISO date.

        Defaults to today.

        By default (verbose=False) the response is trimmed to the analytically
        useful summary: sleep duration, stage breakdown, sleep score, sleep
        need, plus a few top-level signals (avgOvernightHrv, restingHeartRate,
        bodyBatteryChange, …). The per-minute / per-5-min streams
        (sleepMovement, sleepHeartRate, hrvData.hrvReadings,
        wellnessEpochRespirationDataDTOList, …) and algorithm internals
        (id, userProfilePK, sleepFromDevice, sleepVersion, …) are dropped —
        together they account for ~95% of the upstream payload but power no
        analytical use case the LLM cares about.

        Pass verbose=True to get the un-modified upstream response for raw
        stream access. The cache stores the full upstream regardless, so a
        later verbose=True call hits the cache rather than re-fetching.
        """
        full = _fetch_sleep(coerce_date(date))
        return full if verbose else trim_sleep(full)

    @mcp.tool()
    @audited
    @safe_call
    @cached(ttl=TTL_WELLNESS)
    def get_hrv(date: str | None = None) -> dict[str, Any]:
        """Heart-rate variability summary for a given ISO date. Defaults to today."""
        result = client_factory().get_hrv_data(coerce_date(date))
        return result or {}

    # Cached fetcher for the full upstream body-battery payload. Same
    # pattern as `_fetch_sleep` / `_fetch_stress` — caching happens before
    # trimming so a verbose=True call after a verbose=False call hits the
    # cache rather than re-fetching.
    @cached(ttl=TTL_WELLNESS)
    def _fetch_body_battery(start_date: str, end_date: str) -> list[dict[str, Any]]:
        return client_factory().get_body_battery(start_date, end_date) or []

    @mcp.tool()
    @audited
    @safe_call
    def get_body_battery(
        start_date: str, end_date: str | None = None, verbose: bool = False
    ) -> list[dict[str, Any]]:
        """Body battery readings across a date range (inclusive).

        By default (verbose=False) each per-day entry is trimmed to the
        analytically useful summary: day-level charged/drained totals, the
        compressed transition `bodyBatteryValuesArray` (the 6-12 inflection
        points), per-activity impact events (sleep / exercise / recovery
        with bodyBatteryImpact), and the short-form dynamic-feedback events.
        Descriptor metadata, `userProfilePK`, `bodyBatteryVersion`, verbose
        `feedbackLongType` strings, and per-event device/audit metadata
        (`deviceId`, `eventUpdateTimeGmt`, `timezoneOffset`) are dropped.

        Pass verbose=True to get the un-modified upstream response. The
        cache stores the full upstream regardless, so a later verbose=True
        call hits the cache rather than re-fetching.

        Args:
            start_date: ISO start date.
            end_date: ISO end date (defaults to start_date).
            verbose: If True, return the full upstream payload (default False).
        """
        s = coerce_date(start_date)
        e = coerce_date(end_date) if end_date else s
        full = _fetch_body_battery(s, e)
        return full if verbose else trim_body_battery(full)

    # Cached fetcher for the full upstream stress payload. Same pattern as
    # `_fetch_sleep` — caching happens before trimming so a verbose=True
    # call after a verbose=False call hits the cache rather than re-fetching.
    @cached(ttl=TTL_WELLNESS)
    def _fetch_stress(date: str) -> dict[str, Any]:
        return client_factory().get_stress_data(date)

    @mcp.tool()
    @audited
    @safe_call
    def get_stress(date: str | None = None, verbose: bool = False) -> dict[str, Any]:
        """Stress timeline for a given ISO date. Defaults to today.

        By default (verbose=False) the response is trimmed to the
        analytically useful summary: per-day avg/max stress, duration
        breakdowns (rest / low / medium / high), and a 24-entry
        `stressBuckets` aggregation (one per local-day hour with avgStress,
        maxStress, sampleCount, unmeasuredCount). The 480-sample raw
        `stressValuesArray`, the bundled body-battery sub-payload (which
        has its own dedicated `get_body_battery` tool), chart-layout hints,
        and descriptor metadata are dropped — together they account for
        most of the ~30KB upstream payload but power no analytical use
        case the LLM cares about.

        Pass verbose=True to get the un-modified upstream response. The
        cache stores the full upstream regardless, so a later verbose=True
        call hits the cache rather than re-fetching.
        """
        full = _fetch_stress(coerce_date(date))
        return full if verbose else trim_stress(full)

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
