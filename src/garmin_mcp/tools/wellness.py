"""Wellness MCP tools (sleep, HRV, body battery, stress, steps, daily summary)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from garminconnect import Garmin
from mcp.server.fastmcp import FastMCP

from garmin_mcp.cache import TTL_WELLNESS, cached
from garmin_mcp.tools._helpers import audited, coerce_date, safe_call
from garmin_mcp.tools._trimmers import (
    trim_body_battery,
    trim_daily_summary,
    trim_hrv,
    trim_sleep,
    trim_steps,
    trim_stress,
)

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

    # Cached fetcher for the full upstream HRV payload. Same pattern as
    # `_fetch_sleep` / `_fetch_stress` — caching happens before trimming so
    # a verbose=True call after a verbose=False call hits the cache rather
    # than re-fetching.
    @cached(ttl=TTL_WELLNESS)
    def _fetch_hrv(date: str) -> dict[str, Any]:
        return client_factory().get_hrv_data(date) or {}

    @mcp.tool()
    @audited
    @safe_call
    def get_hrv(date: str | None = None, verbose: bool = False) -> dict[str, Any]:
        """Heart-rate variability summary for a given ISO date. Defaults to today.

        By default (verbose=False) the response is trimmed to the analytically
        useful summary: `hrvSummary` (status, lastNightAvg, weeklyAvg,
        lastNight5MinHigh, baseline, feedbackPhrase) plus the surrounding
        sleep-window timestamps. The ~60 per-5-min entries in `hrvReadings`
        and `userProfilePk` are dropped — together they account for ~90% of
        the upstream payload (measured ~10x reduction on Kamil's
        2026-05-09 data: ~7200 → ~700 chars), but the LLM reasons about
        recovery from the summary, not the raw waveform.

        Pass verbose=True to get the un-modified upstream response for raw
        waveform access. The cache stores the full upstream regardless, so
        a later verbose=True call hits the cache rather than re-fetching.
        """
        full = _fetch_hrv(coerce_date(date))
        return full if verbose else trim_hrv(full)

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
        with bodyBatteryImpact), and the dynamic-feedback events (with
        both short and long feedback codes — the long form sometimes
        carries narrative context not derivable from the short code).
        Descriptor metadata, `userProfilePK`, `bodyBatteryVersion`, and
        per-event device/audit metadata (`deviceId`, `eventUpdateTimeGmt`,
        `timezoneOffset`) are dropped.

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

    # Cached fetcher for the full upstream steps payload. Same pattern as
    # `_fetch_stress` / `_fetch_body_battery` — caching happens before
    # trimming so a verbose=True call after a verbose=False call hits the
    # cache rather than re-fetching.
    @cached(ttl=TTL_WELLNESS)
    def _fetch_steps(date: str) -> list[dict[str, Any]]:
        return client_factory().get_steps_data(date) or []

    @mcp.tool()
    @audited
    @safe_call
    def get_steps(date: str | None = None, verbose: bool = False) -> list[dict[str, Any]]:
        """Step counts (15-min buckets) for a given ISO date. Defaults to today.

        By default (verbose=False) the response is trimmed: `pushes` (always
        0 for non-wheelchair users) and `activityLevelConstant` (algorithm
        internal) are dropped from every bucket, and contiguous runs of
        zero-step buckets that share the same `primaryActivityLevel` are
        collapsed into a single bucket spanning the run. A typical day
        has ~70% zero-step buckets (sleep + sedentary lulls), so this
        usually compresses the 96-bucket upstream to ~58 buckets
        (~2.2x reduction on a typical mixed day; more on a heavy-sleep /
        low-activity day) without losing analytical signal — every
        non-zero bucket and every activity-level transition survives.

        Pass verbose=True to get the un-modified upstream response. The
        cache stores the full upstream regardless, so a later verbose=True
        call hits the cache rather than re-fetching.
        """
        full = _fetch_steps(coerce_date(date))
        return full if verbose else trim_steps(full)

    # Cached fetcher for the full upstream daily-summary payload. Same
    # pattern as `_fetch_sleep` / `_fetch_stress` — caching happens before
    # trimming so a verbose=True call after a verbose=False call hits the
    # cache rather than re-fetching.
    @cached(ttl=TTL_WELLNESS)
    def _fetch_daily_summary(date: str) -> dict[str, Any]:
        return client_factory().get_user_summary(date) or {}

    @mcp.tool()
    @audited
    @safe_call
    def get_daily_summary(date: str | None = None, verbose: bool = False) -> dict[str, Any]:
        """Daily user summary (steps, calories, intensity minutes, RHR, etc.).

        Defaults to today.

        By default (verbose=False) the response is trimmed to the analytically
        useful summary: every numeric daily total (calories, steps, distance,
        intensity minutes, floors), the active/sedentary/sleeping seconds,
        the heart-rate summary (resting / min / max / 7-day avg), the stress
        block (durations + percentages + qualifier), the body-battery summary
        values, the respiration summary, and the wellness window timestamps.
        Dropped:

          - Cross-tool body-battery duplicates (`bodyBatteryActivityEventList`,
            `bodyBatteryDynamicFeedbackEvent`,
            `endOfDayBodyBatteryDynamicFeedbackEvent`) — `get_body_battery`
            is the dedicated tool.
          - Wellness aliases (`wellnessKilocalories`,
            `wellnessActiveKilocalories`, `wellnessDistanceMeters`) — equal
            to the `total*` / `active*` copies.
          - PII / identifiers / version / privacy / sync metadata
            (`userProfileId`, `uuid`, `rule`, `bodyBatteryVersion`, …).
          - Spo2 fields when every reading is null. A populated Spo2 block
            survives.
          - `abnormalHeartRateAlertsCount` when null (a non-null value is a
            medical signal and survives).

        Pass verbose=True to get the un-modified upstream response. The
        cache stores the full upstream regardless, so a later verbose=True
        call hits the cache rather than re-fetching.
        """
        full = _fetch_daily_summary(coerce_date(date))
        return full if verbose else trim_daily_summary(full)
