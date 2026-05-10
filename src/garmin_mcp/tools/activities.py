"""Activity-related MCP tools."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from garminconnect import Garmin
from mcp.server.fastmcp import FastMCP

from garmin_mcp.cache import TTL_ACTIVITY_FINAL, TTL_ACTIVITY_LIST, cached
from garmin_mcp.tools._helpers import audited, coerce_date, safe_call
from garmin_mcp.tools._trimmers import (
    trim_activity_detail,
    trim_activity_splits,
    trim_activity_summary,
)

ClientFactory = Callable[[], Garmin]


def register(mcp: FastMCP, client_factory: ClientFactory) -> None:
    # Cached fetcher for the raw activity list. The cache lives *here*
    # (before trimming) so a verbose=True call after a verbose=False call
    # hits the cache rather than re-fetching from Garmin — same pattern
    # as the wellness tools.
    @cached(ttl=TTL_ACTIVITY_LIST)
    def _fetch_recent_activities(limit: int, start: int) -> list[dict[str, Any]]:
        result = client_factory().get_activities(start, limit)
        # The SDK returns either list or dict depending on endpoint shape; normalise.
        if isinstance(result, dict):
            return result.get("activityList", []) or []
        return result or []

    @mcp.tool()
    @audited
    @safe_call
    def list_recent_activities(
        limit: int = 20, start: int = 0, verbose: bool = False
    ) -> list[dict[str, Any]]:
        """List the user's most recent activities, newest first.

        By default (verbose=False) each summary item is trimmed of:
          - the ~30-entry `userRoles` OAuth scope list (duplicated on
            every item)
          - owner identity + profile-image URLs (`ownerId`,
            `ownerDisplayName`, `ownerFullName`, three S3
            `ownerProfileImageUrl*` URLs)
          - constant-value UI hints + privacy stub (`privacy`, `userPro`,
            `hasVideo`, `hasImages`, `hasHeatMap`, `hasIntensityIntervals`,
            `hasSplits`, `hasPolyline`)
          - default-valued booleans (`manufacturer`, `parent`, `decoDive`,
            `qualifyingDive`, `atpActivity`, `manualActivity`,
            `purposeful`, `favorite`, `pr`, `autoCalcCalories`,
            `elevationCorrected`)
          - redundant identifiers (`timeZoneId`, `activityUUID`)
          - empty `summarizedDiveInfo` / `splitSummaries` wrappers

        Together these account for ~80% of a typical summary item. The
        analytical signal (id, name, type, timestamps, duration, distance,
        HR, calories, training effect, sport-specific stats, hrTimeInZone_*,
        powerTimeInZone_*, summarizedExerciseSets, populated splitSummaries)
        passes through untouched.

        Pass verbose=True to get the un-modified upstream response. The
        cache stores the full upstream regardless, so a later verbose=True
        call hits the cache rather than re-fetching.

        Args:
            limit: Number of activities to return (default 20).
            start: Pagination offset (default 0).
            verbose: If True, return the full upstream payload (default False).
        """
        full = _fetch_recent_activities(limit, start)
        return full if verbose else trim_activity_summary(full)

    # Cached fetcher for the raw single-activity payload.
    @cached(ttl=TTL_ACTIVITY_FINAL)
    def _fetch_activity(activity_id: int) -> dict[str, Any]:
        return client_factory().get_activity(str(activity_id))

    @mcp.tool()
    @audited
    @safe_call
    def get_activity(activity_id: int, verbose: bool = False) -> dict[str, Any]:
        """Fetch full details for a single activity by ID.

        By default (verbose=False) the response is trimmed to the
        analytically dense subset: `activityId`, `activityName`,
        `activityTypeDTO`, `eventTypeDTO`, `summaryDTO` (the durations /
        distance / calories / HR / training effect / sport-specific
        block), `timeZoneUnitDTO`, and a filtered `metadataDTO` (only
        `lapCount` + the upload / update timestamps). Dropped:
        `accessControlRuleDTO` (privacy stub), `activityUUID`
        (redundant with `activityId`), `isMultiSportParent`,
        `userProfileId`, and the bulk of `metadataDTO` (device / audit
        metadata, `userInfoDto` PII + image URLs, the `eBike*` block,
        UI-hint booleans).

        Pass verbose=True to get the un-modified upstream response. The
        cache stores the full upstream regardless, so a later verbose=True
        call hits the cache rather than re-fetching.
        """
        full = _fetch_activity(activity_id)
        return full if verbose else trim_activity_detail(full)

    # Cached fetcher for the raw splits payload.
    @cached(ttl=TTL_ACTIVITY_FINAL)
    def _fetch_activity_splits(activity_id: int) -> dict[str, Any]:
        return client_factory().get_activity_splits(str(activity_id))

    @mcp.tool()
    @audited
    @safe_call
    def get_activity_splits(activity_id: int, verbose: bool = False) -> dict[str, Any]:
        """Per-split (lap) breakdown for an activity.

        By default (verbose=False) the response is trimmed of `eventDTOs`
        (TIMER_TRIGGER / LAP_TRIGGER markers from auto-pause / manual-lap
        presses — chart-rendering noise the LLM never reasons over), and
        each lap's empty `lengthDTOs` / `connectIQMeasurement` lists are
        dropped (those populate only for swim / Connect IQ activities).
        Populated `lengthDTOs` / `connectIQMeasurement` pass through
        unchanged.

        Pass verbose=True to get the un-modified upstream response. The
        cache stores the full upstream regardless, so a later verbose=True
        call hits the cache rather than re-fetching.
        """
        full = _fetch_activity_splits(activity_id)
        return full if verbose else trim_activity_splits(full)

    @mcp.tool()
    @audited
    @safe_call
    @cached(ttl=TTL_ACTIVITY_FINAL)
    def get_activity_hr_zones(activity_id: int) -> list[dict[str, Any]]:
        """Time-in-HR-zone breakdown for an activity (one entry per zone)."""
        return client_factory().get_activity_hr_in_timezones(str(activity_id)) or []

    # Cached fetcher for the raw search-by-type payload.
    @cached(ttl=TTL_ACTIVITY_LIST)
    def _fetch_activities_by_type(
        type_: str, start_date: str, end_date: str, limit: int
    ) -> list[dict[str, Any]]:
        results = client_factory().get_activities_by_date(start_date, end_date, activitytype=type_)
        return (results or [])[:limit]

    @mcp.tool()
    @audited
    @safe_call
    def search_activities_by_type(
        type: str,
        start_date: str,
        end_date: str,
        limit: int = 50,
        verbose: bool = False,
    ) -> list[dict[str, Any]]:
        """Search activities of a given type within a date range.

        Returns the same per-activity summary shape as
        `list_recent_activities`, so the same trim applies — see that tool
        for the exact drop list. Pass verbose=True to get the un-modified
        upstream response (the cache stores the full upstream regardless).

        Args:
            type: Garmin activity type (e.g. "running", "cycling", "swimming").
            start_date: Inclusive ISO start date (YYYY-MM-DD).
            end_date: Inclusive ISO end date (YYYY-MM-DD).
            limit: Maximum activities to return (default 50).
            verbose: If True, return the full upstream payload (default False).
        """
        s = coerce_date(start_date)
        e = coerce_date(end_date)
        full = _fetch_activities_by_type(type, s, e, limit)
        return full if verbose else trim_activity_summary(full)
