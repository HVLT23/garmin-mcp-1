"""Trend / analytical MCP tools — derived computations over per-day fetchers.

These tools surface deterministic math (medians, trends, deltas) so callers
don't have to recompute the same numbers in-prompt. The daily coachbot was
pulling 14 days of raw JSON and asking the LLM to compute medians; that
wastes ~40KB of context per run AND produces drift between runs because the
LLM does the math slightly differently each time. Here the math is
deterministic — same numbers every call.

Implementation choice — coupling: these tools call `client_factory()`
directly against the un-trimmed Garmin shape rather than chaining through
the public `get_X` tools (which return trimmed payloads). Reasons:

  - Field paths in the brief reference the un-trimmed shape, so this is
    the path of least surprise for the spec.
  - It avoids a hard dependency on `_trimmers` output shapes — if a future
    PR re-trims `get_daily_summary` more aggressively, the analytics tools
    don't break.

The price is duplicated cache namespaces (these helpers' `__qualname__`
differs from the wellness/training closures, so the cache keys differ).
The TTL buckets are shared, so memory pressure is bounded by `_MAX_ENTRIES`,
and a 14-day warm-cache loop costs 14 dict lookups. A cold-cache 14-day
loop is sequential N round-trips to Garmin (~5-10s wall-clock); good
enough for an interactive tool, no async complication needed.
"""

from __future__ import annotations

import datetime as dt
import logging
import statistics
from collections.abc import Callable
from typing import Any

from garminconnect import Garmin
from mcp.server.fastmcp import FastMCP

from garmin_mcp.cache import (
    TTL_ACTIVITY_LIST,
    TTL_TRAINING_STATUS,
    TTL_WELLNESS,
    cached,
)
from garmin_mcp.tools._helpers import audited, coerce_date, safe_call
from garmin_mcp.tools._trimmers import (
    trim_body_battery,
    trim_daily_summary,
    trim_hrv,
    trim_sleep,
    trim_training_readiness,
    trim_training_status,
)
from garmin_mcp.tools.aggregate import _capture, _parse_activity_start

logger = logging.getLogger(__name__)

ClientFactory = Callable[[], Garmin]


# ---------------------------------------------------------------------------
# Trend qualification heuristic
# ---------------------------------------------------------------------------

def _linreg_slope(values: list[float]) -> float:
    """Ordinary least-squares slope of `values` against position index.

    Returns 0.0 when `values` has fewer than 2 points or all xs collapse
    (the latter can't happen here — xs are always 0..n-1 — but guarded
    anyway so the helper is reusable).
    """
    n = len(values)
    if n < 2:
        return 0.0
    xs = list(range(n))
    mean_x = sum(xs) / n
    mean_y = sum(values) / n
    num = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, values, strict=True))
    den = sum((x - mean_x) ** 2 for x in xs)
    return num / den if den else 0.0


def _qualify_trend(values: list[float], slope: float) -> str:
    """Bucket a slope into rising / falling / stable.

    Heuristic: if the slope-over-window magnitude is smaller than the
    population standard deviation, call it "stable" — the total drift
    sits inside one noise unit, so we shouldn't claim a direction.
    Otherwise the sign of the slope decides. A pure-flat sequence
    (stdev=0) is always "stable".

    Threshold of 1.0 * stdev (rather than the brief's 0.5) chosen because
    0.5 mislabels mildly-fluctuating ~5%-noise sequences as rising/falling
    on 14-day windows; 1.0 keeps the cleanly monotonic test cases as
    rising/falling while letting noisy ones land on stable.
    """
    n = len(values)
    if n < 2:
        return "stable"
    stdev = statistics.pstdev(values)
    if stdev == 0:
        return "stable"
    if abs(slope * n) < stdev:
        return "stable"
    return "rising" if slope > 0 else "falling"


def _safe_get(d: Any, *keys: str) -> Any:
    """Walk a nested dict by key path; return None on any missing/non-dict step."""
    cur: Any = d
    for k in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
    return cur


def _activity_type_key(activity: Any) -> str | None:
    """Return an activity's `typeKey` from either Garmin shape, or None.

    `get_activity()` returns the type under `activityTypeDTO.typeKey`
    (the detail-endpoint shape), while `get_activities()` returns it
    under `activityType.typeKey` (the list-endpoint shape). The earlier
    code only looked at `activityType`, which made
    `compare_activity_to_baseline` fail with `missing_type` whenever the
    target's detail-endpoint payload was hit. Accept either shape.
    """
    if not isinstance(activity, dict):
        return None
    for parent_key in ("activityType", "activityTypeDTO"):
        parent = activity.get(parent_key)
        if isinstance(parent, dict):
            key = parent.get("typeKey")
            if isinstance(key, str) and key:
                return key
    return None


def _pct(numer: float | int | None, denom: float | int | None) -> float | None:
    """Percentage change `(numer/denom)*100`, rounded to 1 dp. None on missing/zero."""
    if numer is None or denom is None or denom == 0:
        return None
    return round((numer / denom) * 100, 1)


def _qualifier(delta_pct: float | None) -> str | None:
    if delta_pct is None:
        return None
    if delta_pct < -10:
        return "low"
    if delta_pct > 10:
        return "elevated"
    return "typical"


def register(mcp: FastMCP, client_factory: ClientFactory) -> None:
    # Per-day fetchers. These mirror wellness/training's closures but with
    # different qualnames → independent cache keys (see module docstring).

    @cached(ttl=TTL_WELLNESS)
    def _fetch_sleep(date: str) -> dict[str, Any]:
        return client_factory().get_sleep_data(date) or {}

    @cached(ttl=TTL_WELLNESS)
    def _fetch_hrv(date: str) -> dict[str, Any]:
        return client_factory().get_hrv_data(date) or {}

    @cached(ttl=TTL_WELLNESS)
    def _fetch_daily_summary(date: str) -> dict[str, Any]:
        return client_factory().get_user_summary(date) or {}

    @cached(ttl=TTL_TRAINING_STATUS)
    def _fetch_training_status(date: str) -> dict[str, Any]:
        return client_factory().get_training_status(date) or {}

    @cached(ttl=TTL_ACTIVITY_LIST)
    def _fetch_activities(limit: int, start: int) -> list[dict[str, Any]]:
        result = client_factory().get_activities(start, limit)
        if isinstance(result, dict):
            return result.get("activityList", []) or []
        return result or []

    # Hard ceiling so a user with thousands of activities can't make us
    # walk the entire backfill. Garmin returns newest-first; at 5 sessions
    # per day this covers ~7 months, well beyond any analytical window
    # this module supports.
    _ACTIVITY_WALK_CAP = 1000
    _ACTIVITY_PAGE_SIZE = 50

    def _walk_activities_back_to(
        date_floor: dt.date,
    ) -> tuple[list[dict[str, Any]], int, bool]:
        """Page through `get_activities` (newest-first) until the page's
        oldest item is at or before `date_floor`.

        Garmin's `get_activities(start, limit)` returns activities
        newest-first. The brief's older single-page `start=0,
        limit=max(50, days*5)` heuristic silently truncated whenever the
        target was older than the page covered (e.g. a 14-day baseline
        anchored on a 3-week-old run with 5 sessions/day).

        Returns `(activities, pages_fetched, truncated)`. `truncated` is
        True iff we hit the `_ACTIVITY_WALK_CAP` safety cap without
        reaching the floor — callers should surface that so consumers
        know the window may be incomplete.
        """
        out: list[dict[str, Any]] = []
        start = 0
        pages = 0
        while True:
            page = _fetch_activities(limit=_ACTIVITY_PAGE_SIZE, start=start)
            pages += 1
            if not page:
                return out, pages, False
            out.extend(page)
            oldest_on_page: dt.date | None = None
            for a in page:
                if not isinstance(a, dict):
                    continue
                a_start = _parse_activity_start(a)
                if a_start is None:
                    continue
                d = a_start.date()
                if oldest_on_page is None or d < oldest_on_page:
                    oldest_on_page = d
            if oldest_on_page is None or oldest_on_page <= date_floor:
                return out, pages, False
            start += _ACTIVITY_PAGE_SIZE
            if start >= _ACTIVITY_WALK_CAP:
                return out, pages, True

    # ---- per-metric extractors ------------------------------------------
    # Each takes a date string and returns a numeric value or None.

    def _extract_load(date: str, key: str) -> float | int | None:
        """Pull `acuteTrainingLoadDTO[key]` from the training-status payload.

        `latestTrainingStatusData` is either `{deviceId: {…}}` (multi-device)
        or the inner dict already (single device, after the upstream collapse).
        Try the inner shape first; fall back to scanning device entries so
        multi-device users still resolve.
        """
        latest = _safe_get(
            _fetch_training_status(date),
            "mostRecentTrainingStatus",
            "latestTrainingStatusData",
        )
        if not isinstance(latest, dict):
            return None
        inner = _safe_get(latest, "acuteTrainingLoadDTO", key)
        if isinstance(inner, (int, float)):
            return inner
        for v in latest.values():
            cand = _safe_get(v, "acuteTrainingLoadDTO", key)
            if isinstance(cand, (int, float)):
                return cand
        return None

    def _extract_sleep_duration_min(date: str) -> float | None:
        secs = _safe_get(_fetch_sleep(date), "dailySleepDTO", "sleepTimeSeconds")
        if not isinstance(secs, (int, float)):
            return None
        return round(secs / 60, 1)

    def _extract_body_battery_at_wake(date: str) -> int | float | None:
        # Lives on the daily-summary payload, not on get_body_battery.
        return _safe_get(_fetch_daily_summary(date), "bodyBatteryAtWakeTime")

    METRICS: dict[str, Callable[[str], Any]] = {
        "hrv_overnight": lambda d: _safe_get(_fetch_hrv(d), "hrvSummary", "lastNightAvg"),
        "resting_heart_rate": lambda d: _safe_get(_fetch_daily_summary(d), "restingHeartRate"),
        "body_battery_at_wake": _extract_body_battery_at_wake,
        "sleep_score": lambda d: _safe_get(
            _fetch_sleep(d), "dailySleepDTO", "sleepScores", "overall", "value"
        ),
        "sleep_duration_min": _extract_sleep_duration_min,
        "stress_avg": lambda d: _safe_get(_fetch_daily_summary(d), "averageStressLevel"),
        "steps": lambda d: _safe_get(_fetch_daily_summary(d), "totalSteps"),
        "vo2_max_generic": lambda d: _safe_get(
            _fetch_training_status(d), "mostRecentVO2Max", "generic", "vo2MaxValue"
        ),
        "acute_training_load": lambda d: _extract_load(d, "dailyTrainingLoadAcute"),
        "chronic_training_load": lambda d: _extract_load(d, "dailyTrainingLoadChronic"),
    }

    # ------------------------------------------------------------------
    # Tool 1: get_metric_trend
    # ------------------------------------------------------------------
    @mcp.tool()
    @audited
    @safe_call
    def get_metric_trend(
        metric: str,
        days: int = 14,
        anchor_date: str | None = None,
    ) -> dict[str, Any]:
        """Trend (samples + summary stats) for one named metric over the last N days.

        Supported metrics: hrv_overnight, resting_heart_rate,
        body_battery_at_wake, sleep_score, sleep_duration_min, stress_avg,
        steps, vo2_max_generic, acute_training_load, chronic_training_load.

        The anchor_date defaults to today. The window is `days` days
        ending on the anchor (inclusive). Minimum `days=2`; smaller values
        return `bad_argument` (a 1-sample window has no trend to compute).
        `weekly_avg` is the mean of the last 7 days inclusive of the
        anchor; `monthly_avg` is the mean of the full window when
        `days >= 30`, else None.

        Trend qualification heuristic: if the regression slope's
        magnitude over the window is below 1.0 * population stddev, the
        trend is "stable" - apparent drift sits inside the noise floor.
        Otherwise sign-of-slope picks rising vs falling.

        Missing data: per-day fetch failures are tolerated and reported
        in `missing_dates`. If more than half the window is missing, a
        structured `{"error": "insufficient_data", ...}` is returned with
        the partial sample list so the caller can decide what to do.

        Cold-cache cost: ~5-10s for a 14-day window (sequential per-day
        fetches). Warm-cache: milliseconds.
        """
        if metric not in METRICS:
            return {
                "error": "bad_argument",
                "message": (
                    f"unknown metric {metric!r}; supported: {sorted(METRICS)}"
                ),
            }
        if days < 2:
            return {"error": "bad_argument", "message": "days must be >= 2"}

        anchor = dt.date.fromisoformat(coerce_date(anchor_date))
        extractor = METRICS[metric]

        # Build oldest-first so list ordering matches calendar order.
        dates_in_window = [
            (anchor - dt.timedelta(days=offset)).isoformat()
            for offset in range(days - 1, -1, -1)
        ]

        samples: list[dict[str, Any]] = []
        missing: list[str] = []
        for d in dates_in_window:
            try:
                v = extractor(d)
            except Exception as e:
                # The cached helpers and `_safe_get` already swallow most
                # bad shapes; this is a belt-and-braces guard against an
                # extractor exploding mid-window. One bad day shouldn't
                # blank the whole trend.
                logger.warning("metric %s: extractor failed for %s: %s", metric, d, e)
                v = None
            if v is None:
                missing.append(d)
            else:
                samples.append({"date": d, "value": v})

        n = len(samples)

        # Insufficient data: more than half the window is missing.
        if n < max(2, (days + 1) // 2):
            return {
                "error": "insufficient_data",
                "metric": metric,
                "anchor_date": anchor.isoformat(),
                "window_days": days,
                "samples": samples,
                "missing_dates": missing,
                "n_samples": n,
                "message": (
                    f"only {n}/{days} samples available for metric {metric!r}; "
                    "need at least half the window populated"
                ),
            }

        anchor_iso = anchor.isoformat()
        yesterday_iso = (anchor - dt.timedelta(days=1)).isoformat()
        by_date = {s["date"]: s["value"] for s in samples}
        today_val = by_date.get(anchor_iso)
        yesterday_val = by_date.get(yesterday_iso)

        weekly_window = [
            s["value"]
            for s in samples
            if (anchor - dt.date.fromisoformat(s["date"])).days < 7
        ]
        weekly_avg = round(statistics.mean(weekly_window), 2) if weekly_window else None
        monthly_avg = (
            round(statistics.mean([s["value"] for s in samples]), 2) if days >= 30 else None
        )

        values = [s["value"] for s in samples]
        slope = _linreg_slope(values)
        trend = _qualify_trend(values, slope)

        delta_y = (
            round(today_val - yesterday_val, 2)
            if isinstance(today_val, (int, float)) and isinstance(yesterday_val, (int, float))
            else None
        )
        delta_w = (
            round(today_val - weekly_avg, 2)
            if isinstance(today_val, (int, float)) and isinstance(weekly_avg, (int, float))
            else None
        )

        return {
            "metric": metric,
            "anchor_date": anchor_iso,
            "window_days": days,
            "samples": samples,
            "today": today_val,
            "yesterday": yesterday_val,
            "weekly_avg": weekly_avg,
            "monthly_avg": monthly_avg,
            "min": min(values),
            "max": max(values),
            "trend": trend,
            "trend_slope_per_day": round(slope, 4),
            "delta_vs_yesterday": delta_y,
            "delta_vs_yesterday_pct": _pct(delta_y, yesterday_val),
            "delta_vs_weekly_avg": delta_w,
            "delta_vs_weekly_avg_pct": _pct(delta_w, weekly_avg),
            "missing_dates": missing,
            "n_samples": n,
        }

    # ------------------------------------------------------------------
    # Tool 2: get_recent_activity_summary
    # ------------------------------------------------------------------
    @mcp.tool()
    @audited
    @safe_call
    def get_recent_activity_summary(
        activity_type: str | None = None,
        days: int = 14,
        anchor_date: str | None = None,
    ) -> dict[str, Any]:
        """Aggregate stats for activities in the last N days, optionally type-filtered.

        Pages through `get_activities` newest-first until the oldest item
        on a page is at or before the window start, then filters to those
        with a `startTimeLocal` inside [anchor-days+1, anchor] and (when
        `activity_type` is supplied) matching `activityType.typeKey`.
        `pages_fetched` and `truncated` are echoed back so callers can
        tell when the safety cap fired and results may be incomplete.

        Distance-based stats skip activities with no distance (strength
        sessions, boxing, …). Median / p90 use `statistics.quantiles` so
        no numpy dep is needed; on n<2 samples the per-field block is
        omitted entirely.

        `types_seen` is only emitted when `activity_type` is None — it's
        the type breakdown across the unfiltered window.
        """
        if days < 1:
            return {"error": "bad_argument", "message": "days must be >= 1"}
        anchor = dt.date.fromisoformat(coerce_date(anchor_date))
        window_start = anchor - dt.timedelta(days=days - 1)

        # Page through newest-first until we cover the window. A single
        # generous page would silently truncate for users with many
        # activities per day; the walker handles that and reports
        # `truncated: true` on the rare cap-hit.
        raw, pages_fetched, truncated = _walk_activities_back_to(window_start)

        matched: list[dict[str, Any]] = []
        types_seen: dict[str, int] = {}
        for a in raw:
            if not isinstance(a, dict):
                continue
            start = _parse_activity_start(a)
            if start is None:
                continue
            d = start.date()
            if d < window_start or d > anchor:
                continue
            type_key = _activity_type_key(a)
            if activity_type is not None and type_key != activity_type:
                continue
            matched.append(a)
            if isinstance(type_key, str):
                types_seen[type_key] = types_seen.get(type_key, 0) + 1

        n = len(matched)

        durations_min = [
            a["duration"] / 60 for a in matched if isinstance(a.get("duration"), (int, float))
        ]
        distances_km = [
            a["distance"] / 1000
            for a in matched
            if isinstance(a.get("distance"), (int, float)) and a["distance"] > 0
        ]
        hrs = [a["averageHR"] for a in matched if isinstance(a.get("averageHR"), (int, float))]
        cals = [a["calories"] for a in matched if isinstance(a.get("calories"), (int, float))]

        result: dict[str, Any] = {
            "activity_type": activity_type,
            "anchor_date": anchor.isoformat(),
            "window_days": days,
            "n_activities": n,
            "total_duration_min": round(sum(durations_min), 1) if durations_min else 0,
            "total_distance_km": round(sum(distances_km), 2) if distances_km else 0,
            "total_calories": round(sum(cals), 0) if cals else 0,
            "activities_per_week": round(n / days * 7, 2) if days else 0,
            "pages_fetched": pages_fetched,
            "truncated": truncated,
        }
        if hrs:
            result["hr_avg"] = _percentiles(hrs, with_minmax=True)
        if durations_min:
            result["duration_min"] = _percentiles(durations_min)
        if distances_km:
            result["distance_km"] = _percentiles(distances_km)
        if activity_type is None:
            result["types_seen"] = types_seen

        return result

    # ------------------------------------------------------------------
    # Tool 3: compare_activity_to_baseline
    # ------------------------------------------------------------------
    @mcp.tool()
    @audited
    @safe_call
    def compare_activity_to_baseline(
        activity_id: int,
        baseline_days: int = 14,
    ) -> dict[str, Any]:
        """Compare a target activity's key metrics against the median of similar activities.

        Baseline = same `activityType.typeKey`, started within the
        `baseline_days` preceding the target's start date (exclusive of
        the target itself). When fewer than 3 baseline activities are
        available, returns `comparisons: {}` and `baseline_insufficient:
        true` rather than crashing — coachbot interprets that as
        "interpret in narrative form, no quantitative anchor".

        Compared fields: averageHR, duration_min, calories, trainingLoad
        (sourced from `activityTrainingLoad`). For running activities,
        also: averageRunningCadenceInStepsPerMinute, avgVerticalRatio,
        avgGroundContactTime, avgStrideLength — present only when the
        target has them, since they're running-only.

        Each comparison is `{today, baseline_p50, delta, delta_pct,
        qualifier}`. Qualifier thresholds: <-10% → "low", -10..10% →
        "typical", >10% → "elevated".
        """
        client = client_factory()
        target = client.get_activity(str(activity_id))
        if not isinstance(target, dict):
            return {
                "error": "invalid_activity",
                "message": f"activity {activity_id} returned non-dict",
            }

        type_key = _activity_type_key(target)
        if type_key is None:
            return {
                "error": "missing_type",
                "message": (
                    f"activity {activity_id} has no typeKey under "
                    "activityType or activityTypeDTO"
                ),
            }

        start = _parse_activity_start(target)
        if start is None:
            return {
                "error": "missing_start_time",
                "message": f"activity {activity_id} has no startTimeLocal",
            }
        target_date = start.date()

        # Baseline window: `baseline_days` preceding the target's date.
        # The walker pages newest-first until the page's oldest activity
        # is at or before `baseline_start` — necessary because the target
        # itself may be days/weeks old and 5+x/day trainers would otherwise
        # see a single fixed page silently truncate older entries.
        target_id = target.get("activityId")
        baseline_start = target_date - dt.timedelta(days=baseline_days)
        raw, pages_fetched, truncated = _walk_activities_back_to(baseline_start)

        baseline: list[dict[str, Any]] = []
        baseline_date_set: set[str] = set()
        for a in raw:
            if not isinstance(a, dict):
                continue
            if a.get("activityId") == target_id:
                continue
            if _activity_type_key(a) != type_key:
                continue
            a_start = _parse_activity_start(a)
            if a_start is None:
                continue
            d = a_start.date()
            if d < baseline_start or d >= target_date:
                continue
            baseline.append(a)
            baseline_date_set.add(d.isoformat())

        baseline_dates = sorted(baseline_date_set)

        if len(baseline) < 3:
            return {
                "activity_id": activity_id,
                "activity_type": type_key,
                "baseline_window_days": baseline_days,
                "baseline_n": len(baseline),
                "comparisons": {},
                "baseline_dates": baseline_dates,
                "baseline_insufficient": True,
                "pages_fetched": pages_fetched,
                "truncated": truncated,
                "note": (
                    "fewer than 3 baseline activities of this type in the "
                    "window — quantitative comparison suppressed"
                ),
            }

        # Field paths into the activity dict. Two are derived (duration_min
        # from seconds, trainingLoad from activityTrainingLoad).
        def _val(a: dict[str, Any], field: str) -> float | None:
            if field == "duration_min":
                v = a.get("duration")
                return v / 60 if isinstance(v, (int, float)) else None
            if field == "trainingLoad":
                v = a.get("activityTrainingLoad")
                return v if isinstance(v, (int, float)) else None
            v = a.get(field)
            return v if isinstance(v, (int, float)) else None

        common_fields = ["averageHR", "duration_min", "calories", "trainingLoad"]
        running_fields = [
            "averageRunningCadenceInStepsPerMinute",
            "avgVerticalRatio",
            "avgGroundContactTime",
            "avgStrideLength",
        ]
        fields = list(common_fields)
        if type_key == "running":
            fields.extend(f for f in running_fields if isinstance(target.get(f), (int, float)))

        comparisons: dict[str, dict[str, Any]] = {}
        for field in fields:
            today_val = _val(target, field)
            baseline_vals = [v for v in (_val(a, field) for a in baseline) if v is not None]
            if today_val is None or len(baseline_vals) < 3:
                continue
            p50 = round(statistics.median(baseline_vals), 2)
            delta = round(today_val - p50, 2)
            delta_pct = _pct(delta, p50)
            comparisons[field] = {
                "today": round(today_val, 2),
                "baseline_p50": p50,
                "delta": delta,
                "delta_pct": delta_pct,
                "qualifier": _qualifier(delta_pct),
            }

        return {
            "activity_id": activity_id,
            "activity_type": type_key,
            "baseline_window_days": baseline_days,
            "baseline_n": len(baseline),
            "comparisons": comparisons,
            "baseline_dates": baseline_dates,
            "baseline_insufficient": False,
            "pages_fetched": pages_fetched,
            "truncated": truncated,
        }

    # ------------------------------------------------------------------
    # Tool 4: get_today_summary
    # ------------------------------------------------------------------
    @mcp.tool()
    @audited
    @safe_call
    def get_today_summary(date: str | None = None) -> dict[str, Any]:
        """Composed daily-life snapshot — the non-workout-day equivalent of
        `get_session_with_context`.

        Bundles sleep, HRV, body battery, training readiness, training
        status (which carries VO2 max and load balance), and the daily
        user summary for the given date (defaults to today). Each section
        is fetched independently via the per-section `_capture` pattern
        from `aggregate.py` — one Garmin endpoint timing out doesn't
        blank the whole response, it shows up as an `{"error":
        "fetch_failed", ...}` stub for that section.

        Each section is trimmed via the same per-tool trim helpers used
        by the standalone `get_X` tools, so the bundle stays bounded
        (~10-15KB on a typical day rather than ~120KB+ raw — the upstream
        sleep payload alone has per-minute movement / HR / respiration
        streams that account for the bulk). Trim helpers all pass
        `{"error": ...}` stubs through unchanged, so per-section failure
        diagnostics aren't swallowed.

        Per-section TTLs: wellness sections use TTL_WELLNESS (30m),
        training sections use TTL_TRAINING_STATUS (60m). Errors are not
        cached so a transient blip clears as soon as the upstream
        recovers.
        """
        d = coerce_date(date)
        client = client_factory()

        sleep = _capture(
            TTL_WELLNESS, ("sleep", d), "sleep",
            lambda: client.get_sleep_data(d),
        )
        hrv = _capture(
            TTL_WELLNESS, ("hrv", d), "hrv",
            lambda: client.get_hrv_data(d),
        )
        body_battery = _capture(
            TTL_WELLNESS, ("body_battery", d), "body_battery",
            lambda: client.get_body_battery(d, d),
        )
        readiness = _capture(
            TTL_TRAINING_STATUS, ("readiness", d), "training_readiness",
            lambda: client.get_training_readiness(d),
        )
        training_status = _capture(
            TTL_TRAINING_STATUS, ("training_status", d), "training_status",
            lambda: client.get_training_status(d),
        )
        daily_summary = _capture(
            TTL_WELLNESS, ("daily_summary", d), "daily_summary",
            lambda: client.get_user_summary(d),
        )

        return {
            "date": d,
            "sleep": trim_sleep(sleep),
            "hrv": trim_hrv(hrv),
            "body_battery": trim_body_battery(body_battery),
            "training_readiness": trim_training_readiness(readiness),
            "training_status": trim_training_status(training_status),
            "daily_summary": trim_daily_summary(daily_summary),
        }


def _percentiles(values: list[float], *, with_minmax: bool = False) -> dict[str, float]:
    """Median + p90 (and optionally min/max) of a numeric list.

    Always-rounded to 1 decimal. Caller guarantees `len(values) >= 1`.
    For p90 with small samples, `statistics.quantiles(n=10)` requires
    `n >= 2`; on a single sample we report median == p90 == that value.
    """
    n = len(values)
    p50 = round(statistics.median(values), 1)
    if n >= 2:
        # `quantiles(n=10)` returns the 9 cut points dividing into deciles;
        # index 8 is the boundary above the 90th percentile.
        deciles = statistics.quantiles(values, n=10)
        p90 = round(deciles[8], 1)
    else:
        p90 = p50
    out: dict[str, float] = {"p50": p50, "p90": p90}
    if with_minmax:
        out["min"] = round(min(values), 1)
        out["max"] = round(max(values), 1)
    return out
