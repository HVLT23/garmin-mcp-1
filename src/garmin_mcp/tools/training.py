"""Training-status MCP tools."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from garminconnect import Garmin
from mcp.server.fastmcp import FastMCP

from garmin_mcp.cache import TTL_TRAINING_STATUS, cached
from garmin_mcp.tools._helpers import audited, coerce_date, safe_call
from garmin_mcp.tools._trimmers import (
    trim_training_load,
    trim_training_readiness,
    trim_training_status,
    trim_vo2_max,
)

ClientFactory = Callable[[], Garmin]


def register(mcp: FastMCP, client_factory: ClientFactory) -> None:
    # Cached fetcher for the full upstream training-status payload. Caching
    # happens *before* trimming so a verbose=True call after a verbose=False
    # call hits the cache rather than re-fetching from Garmin. The fetcher
    # is also re-used by `get_training_load` and `get_vo2_max`, both of
    # which extract sub-blocks from the same upstream — three tools share
    # one Garmin call when invoked for the same date.
    @cached(ttl=TTL_TRAINING_STATUS)
    def _fetch_training_status(date: str) -> dict[str, Any]:
        return client_factory().get_training_status(date) or {}

    @mcp.tool()
    @audited
    @safe_call
    def get_training_status(
        date: str | None = None, verbose: bool = False
    ) -> dict[str, Any]:
        """Garmin training status (productive, maintaining, detraining, etc.).

        Defaults to today.

        By default (verbose=False) the response is trimmed:
        `recordedDevices` (cross-tool repeat) and `showSelector` (UI hint)
        drop unconditionally. The four always-null per-device load fields
        (`weeklyTrainingLoad`, `loadTunnelMin`/`Max`, `loadLevelTrend`)
        and the always-null top-level `heatAltitudeAcclimationDTO` (the
        populated copy lives at `mostRecentVO2Max.heatAltitudeAcclimation`)
        drop only when null — a future Garmin shape change that puts
        real data here flows through rather than being silently swallowed.
        `primaryTrainingDevice` is dropped only on the single-device
        collapse path (always `True` and noise on a single watch); on
        multi-device users it survives as the only signal distinguishing
        primary from secondary watches. Single-device
        `{<deviceId>: {...}}` maps under `latestTrainingStatusData` and
        `metricsTrainingLoadBalanceDTOMap` collapse to the inner dict;
        multi-device maps survive. PII / `*Local` / image URLs drop at
        any depth via the cross-cutting final pass.

        Pass verbose=True to get the un-modified upstream response. The
        cache stores the full upstream regardless, so a later verbose=True
        call hits the cache rather than re-fetching.
        """
        full = _fetch_training_status(coerce_date(date))
        return full if verbose else trim_training_status(full)

    @mcp.tool()
    @audited
    @safe_call
    def get_training_load(
        date: str | None = None, verbose: bool = False
    ) -> dict[str, Any]:
        """Load-balance metrics only (monthly aerobic-low/-high/anaerobic, targets, feedback phrase).

        For broader training status, use `get_training_status`. For VO2
        max, use `get_vo2_max`. All three tools share the same upstream
        cache, so calling them for the same date triggers exactly one
        Garmin request.

        Defaults to today.

        By default (verbose=False) the response is scoped to
        `mostRecentTrainingLoadBalance` only — the previous behaviour
        also returned `mostRecentTrainingStatus` and `mostRecentVO2Max`,
        which were strict-superset duplicates of the dedicated tools.
        `deviceId` drops on every entry; `primaryTrainingDevice` drops
        only on the single-device collapse path (signal-bearing on
        multi-device users). The single-device
        `metricsTrainingLoadBalanceDTOMap` collapses to the inner dict;
        multi-device users keep the map. PII drops via the cross-cutting
        final pass.

        Pass verbose=True to get the un-modified upstream response (the
        full training-status payload, identical to
        `get_training_status(verbose=True)`). The cache is shared across
        all three tools.
        """
        full = _fetch_training_status(coerce_date(date))
        return full if verbose else trim_training_load(full)

    @mcp.tool()
    @audited
    @safe_call
    def get_vo2_max(date: str | None = None, verbose: bool = False) -> dict[str, Any]:
        """Latest VO2 max estimate(s) — generic/running and cycling, if available.

        Defaults to today. Sources from `get_training_status` under the
        hood: the legacy `get_max_metrics` Garmin endpoint returns `[]`
        for users on newer Connect versions, but the data still flows
        through `mostRecentVO2Max` on the training-status response. The
        cache is shared with `get_training_status` and `get_training_load`,
        so calling all three for the same date triggers one Garmin call.

        By default (verbose=False) the response is a tidy
        `{calendarDate, generic, cycling}` dict. `generic.vo2MaxValue`
        (integer) and `generic.vo2MaxPreciseValue` (decimal) both survive
        for narration vs trend analysis. `fitnessAge` /
        `fitnessAgeDescription` (perpetually null) are dropped on every
        discipline; `heatAltitudeAcclimation` is dropped (separate
        concern; available via `get_training_status` for users who need
        it). When the upstream has no VO2 data, an empty `{}` is returned.

        Pass verbose=True to get the un-modified upstream training-status
        payload (identical to `get_training_status(verbose=True)`).
        """
        full = _fetch_training_status(coerce_date(date))
        return full if verbose else trim_vo2_max(full)

    # Cached fetcher for the full upstream training-readiness payload.
    # Same pattern as `_fetch_training_status` — caching before trimming
    # so a verbose=True call after a verbose=False call hits the cache
    # rather than re-fetching from Garmin.
    @cached(ttl=TTL_TRAINING_STATUS)
    def _fetch_training_readiness(date: str) -> list[dict[str, Any]]:
        return client_factory().get_training_readiness(date) or []

    @mcp.tool()
    @audited
    @safe_call
    def get_training_readiness(
        date: str | None = None, verbose: bool = False
    ) -> dict[str, Any] | list[dict[str, Any]]:
        """Training readiness score and the inputs that drove it.

        Defaults to today.

        Some watches log multiple readiness checks per day (one at
        wake-up plus updates whenever real-time variables change); the
        upstream is a list. By default (verbose=False) the trim returns
        just the most-recent reading as a single dict — the LLM almost
        always wants "what's the latest" rather than every check today.

        Per-reading the trim:
          - Renames `recoveryTime` (minutes) to `recoveryTime_min` for
            unit clarity.
          - Collapses the six parallel
            `<factor>FactorPercent`/`<factor>FactorFeedback` pairs into a
            `factors` block of `{score, qualifier}` envelopes (keys:
            `sleep`, `recovery`, `acwr`, `stress`, `hrv`, `sleepHistory`).
          - Drops `deviceId`, `feedbackLong` (verbose code; `feedbackShort`
            is kept), `validSleep`, `inputContext`,
            `primaryActivityTracker`, and the lowercase `timestampLocal`
            duplicate.
          - Drops `recoveryTimeChangePhrase` when null; a non-null phrase
            survives as a meaningful narrative signal.

        API-shape note: the default return is now a single dict (was a
        list). Pass verbose=True to get the un-modified upstream list.
        """
        full = _fetch_training_readiness(coerce_date(date))
        return full if verbose else trim_training_readiness(full)

    @mcp.tool()
    @audited
    @safe_call
    @cached(ttl=TTL_TRAINING_STATUS)
    def get_race_predictor() -> dict[str, Any]:
        """Garmin's race-time predictions across standard distances."""
        return client_factory().get_race_predictions() or {}
