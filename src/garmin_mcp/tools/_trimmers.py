"""Payload trimmers for sleep/HRV/stress/body-battery responses.

Shared between the standalone wellness tools (`get_sleep`, `get_hrv`,
`get_stress`, `get_body_battery`) and the aggregate
`get_session_with_context` tool. Two strategies are exposed for sleep
because the two contexts have different needs:

- `trim_sleep_streams` — drops only the per-minute / per-5-min streams from a
  sleep response. Used by the aggregate tool, which wants the full sleep
  summary alongside an activity for cross-context analysis.
- `trim_sleep` — aggressive allowlist trim for the standalone `get_sleep`
  tool. The standalone tool is asked specifically for sleep data, so we can
  drop algorithm-internal fields (sleepWindowConfirmed, sleepFromDevice,
  ageGroup, …) and keep only the analytically useful summary.

`trim_stress` follows the same idea as `trim_sleep` but uses an explicit
denylist plus an hourly aggregation: the upstream stress response bundles
480 raw 3-min samples + a parallel body-battery payload + chart-layout
hints. We compute one bucket per local hour from the raw samples (avg,
max, sample/unmeasured counts) and drop the streams + body-battery +
chart hints + descriptor lists.

`trim_body_battery` operates over a multi-day list (Garmin returns one
dict per day for a date range). Each per-day dict is filtered with an
allowlist that keeps the day summary, the compressed transition
`bodyBatteryValuesArray`, the activity-tied impact events, and the
dynamic feedback events (both short and long forms — the long form is
sometimes a multi-tag composite that's the densest narrative signal).
Descriptor metadata, `userProfilePK`, and `bodyBatteryVersion` are
dropped.

All strategies treat non-dict input (e.g. error stubs) as a pass-through.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any

# Per-minute / per-5-min stream fields dropped by the aggregate tool's
# lite trim. These dominate the upstream payload but no analytical use
# case consumes them. The standalone `get_sleep` trim drops more via its
# top-level allowlist (see `_SLEEP_TOP_LEVEL_KEEP`).
_SLEEP_STREAM_FIELDS = frozenset(
    {
        "sleepMovement",
        "wellnessEpochRespirationDataDTOList",
        "sleepHeartRate",
        "sleepStress",
        "sleepBodyBattery",
    }
)

# Fields kept inside `dailySleepDTO`. Everything else (id, userProfilePK,
# sleepWindowConfirmed*, sleepFromDevice, sleepResultTypePK, ageGroup,
# sleepVersion, respirationVersion, redundant timestamp aliases,
# skinTempCalibrationDays, …) is algorithm internals or noise.
_DAILY_SLEEP_DTO_KEEP = frozenset(
    {
        "calendarDate",
        "sleepStartTimestampLocal",
        "sleepEndTimestampLocal",
        "sleepStartTimestampGMT",
        "sleepEndTimestampGMT",
        "sleepTimeSeconds",
        "napTimeSeconds",
        "deepSleepSeconds",
        "lightSleepSeconds",
        "remSleepSeconds",
        "awakeSleepSeconds",
        "unmeasurableSleepSeconds",
        "averageRespirationValue",
        "lowestRespirationValue",
        "highestRespirationValue",
        "averageSpo2Value",
        "lowestSpo2Value",
        "awakeCount",
        "avgSleepStress",
        "avgHeartRate",
        "sleepScoreFeedback",
        "sleepScoreInsight",
        "sleepScorePersonalizedInsight",
        "sleepScores",
        "sleepNeed",
        "nextSleepNeed",
        "dailyNapDTOS",
    }
)

# Top-level sleep response fields kept by the aggressive trim. Everything
# else is dropped (including the streams in `_SLEEP_STREAM_FIELDS` and the
# entire `hrvData` block — its summary lives in the top-level `avgOvernightHrv`
# / `hrvStatus` fields).
_SLEEP_TOP_LEVEL_KEEP = frozenset(
    {
        "dailySleepDTO",
        "sleepLevels",
        "restlessMomentsCount",
        "avgOvernightHrv",
        "hrvStatus",
        "bodyBatteryChange",
        "avgSkinTempDeviationC",
        "restingHeartRate",
        "remSleepData",
    }
)

# Algorithm normal-range fields stripped from each sleepScores stage —
# the qualifierKey already says "GOOD"/"FAIR"/etc.
_SLEEP_SCORE_DROP = frozenset(
    {
        "idealStartInSeconds",
        "idealEndInSeconds",
        "optimalStart",
        "optimalEnd",
    }
)


def trim_sleep_streams(sleep: Any) -> Any:
    """Strip per-minute streams from a sleep response (lite trim).

    Keeps `dailySleepDTO` (sleep score, total minutes, stage summary),
    `sleepLevels` (stage windows), and any other top-level summary fields.
    Drops the per-minute streams in `_SLEEP_STREAM_FIELDS` and the nested
    `hrvData.hrvReadings` array. Non-dict inputs (e.g. error stubs) pass
    through unchanged.

    Used by the aggregate tool; the standalone `get_sleep` uses the more
    aggressive `trim_sleep`.
    """
    if not isinstance(sleep, dict):
        return sleep
    trimmed = {k: v for k, v in sleep.items() if k not in _SLEEP_STREAM_FIELDS}
    hrv = trimmed.get("hrvData")
    if isinstance(hrv, dict) and "hrvReadings" in hrv:
        trimmed["hrvData"] = {k: v for k, v in hrv.items() if k != "hrvReadings"}
    return trimmed


def trim_sleep(sleep: Any) -> Any:
    """Aggressive allowlist trim for the standalone `get_sleep` tool.

    Reduces a ~74KB upstream sleep response to ~3KB by:
      - Dropping every per-minute / per-5-min stream (sleepMovement,
        sleepHeartRate, hrvData, …).
      - Filtering `dailySleepDTO` to a small allowlist (sleep duration,
        stages, scores, score insights, SpO2, sleep need).
      - Keeping every `sleepScores` stage (overall + per-stage qualifiers)
        and stripping the algorithm normal ranges (idealStartInSeconds,
        optimalStart, …) from each.
      - Dropping cross-cutting noise (id, userProfilePK,
        sleepWindowConfirmed, ageGroup, sleepVersion, …).

    Non-dict inputs pass through unchanged. Error stubs (dicts with an
    "error" key — produced by the upstream `safe_call`) also pass through,
    so an aggressive allowlist doesn't accidentally swallow the diagnostic.
    """
    if not isinstance(sleep, dict):
        return sleep
    if "error" in sleep:
        return sleep

    trimmed = {k: v for k, v in sleep.items() if k in _SLEEP_TOP_LEVEL_KEEP}

    daily = trimmed.get("dailySleepDTO")
    if isinstance(daily, dict):
        filtered = {k: v for k, v in daily.items() if k in _DAILY_SLEEP_DTO_KEEP}
        scores = filtered.get("sleepScores")
        if isinstance(scores, dict):
            filtered["sleepScores"] = {
                stage: (
                    {k: v for k, v in body.items() if k not in _SLEEP_SCORE_DROP}
                    if isinstance(body, dict)
                    else body
                )
                for stage, body in scores.items()
            }
        trimmed["dailySleepDTO"] = filtered

    return trimmed


def trim_hrv(hrv: Any) -> Any:
    """Strip the per-5-min `hrvReadings` array from an HRV response.

    Keeps the summary block (lastNightAvg, weeklyAvg, status, feedbackPhrase…).
    Non-dict inputs pass through unchanged.
    """
    if not isinstance(hrv, dict):
        return hrv
    return {k: v for k, v in hrv.items() if k != "hrvReadings"}


# Always-drop noise: PII / chart-layout hints / descriptor metadata that
# only matters for rendering the Garmin Connect UI.
_STRESS_DROP_ALWAYS = frozenset(
    {
        "userProfilePK",
        "userProfilePk",
        "stressChartValueOffset",
        "stressChartYAxisOrigin",
        "stressValueDescriptorsDTOList",
    }
)

# Streams + bundled body-battery payload. The stress endpoint apparently
# bundles body-battery into the same response, but body-battery has its
# own dedicated tool — drop it here so we don't double-ship.
_STRESS_DROP_DEFAULT = frozenset(
    {
        "stressValuesArray",
        "bodyBatteryValuesArray",
        "bodyBatteryValueDescriptorsDTOList",
        "bodyBatteryActivityEvent",
        "bodyBatteryDynamicFeedbackEvent",
        "endOfDayBodyBatteryDynamicFeedbackEvent",
    }
)

_HOUR_MS = 3_600_000


def _to_unix_ms(value: Any) -> int | float | None:
    """Coerce a Unix-ms number or ISO 8601 string to Unix-ms.

    The live Garmin stress endpoint returns the surrounding timestamps as
    ISO 8601 strings (e.g. ``'2026-05-08T22:00:00.0'`` — implicit GMT,
    trailing tenth) while the sample timestamps inside ``stressValuesArray``
    are Unix-ms ints. The fixture used by PR #9's tests was Unix-ms
    throughout, masking the asymmetry. This helper normalises both shapes
    and returns ``None`` for unparseable input. Naive datetimes are
    treated as UTC, matching Garmin's GMT-implied encoding.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value)
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return int(dt.timestamp() * 1000)
    return None


def _compute_stress_buckets(
    samples: Any, start_ts: Any, end_ts: Any = None
) -> list[dict[str, Any]] | None:
    """Aggregate 3-min stress samples into one bucket per local-day hour.

    `start_ts` / `end_ts` accept either Unix-ms numbers or ISO 8601 strings
    (the live Garmin endpoint returns the surrounding timestamps as ISO
    strings while the sample timestamps inside `stressValuesArray` stay
    Unix-ms — see `_to_unix_ms`). Both shapes are coerced to Unix-ms
    before bucketing.

    Sample timestamps and `start_ts` must be in the same encoding (both
    GMT-ms or both local-ms-encoded-as-utc — Garmin returns parallel
    `startTimestampGMT` / `startTimestampLocal` fields and the deltas
    between them and the sample timestamps are identical, so the chosen
    encoding doesn't change the resulting hour index).

    The day-window upper bound is derived from `end_ts - start_ts` when
    `end_ts` is provided, so DST fall-back days (25h local day → `hour=24`
    bucket) and spring-forward days (23h local day → no `hour=23` bucket)
    are handled honestly. If `end_ts` is missing or invalid, the bound
    falls back to 24h.

    Returns `None` if the inputs are unusable. Otherwise emits a list of
    dicts, one per hour that has at least one sample, sorted by hour.
    Hours with zero samples are omitted (per spec).

    Garmin marks unmeasured samples with `-1` (no recent HR) or `-2` (no
    contact). They're counted in `unmeasuredCount` but excluded from
    `avgStress` / `maxStress`. NaN levels are also treated as unmeasured
    (rather than crashing `round()` downstream). A bucket whose every
    sample is unmeasured yields `avgStress: None`, `maxStress: None`.
    """
    if not isinstance(samples, list) or not samples:
        return None
    start_ts = _to_unix_ms(start_ts)
    if start_ts is None:
        return None
    end_ts = _to_unix_ms(end_ts)

    max_hour = int((end_ts - start_ts) // _HOUR_MS) if end_ts is not None else 24

    buckets: dict[int, dict[str, Any]] = {}
    for entry in samples:
        if not isinstance(entry, list) or len(entry) < 2:
            continue
        ts, level = entry[0], entry[1]
        if not isinstance(ts, (int, float)):
            continue
        if not isinstance(level, (int, float)) or isinstance(level, bool):
            continue
        hour = int((ts - start_ts) // _HOUR_MS)
        if hour < 0 or hour >= max_hour:
            # Sample falls outside the day window. Skip rather than
            # corrupt the bucket layout.
            continue
        b = buckets.setdefault(hour, {"sum": 0, "count": 0, "max": None, "unmeasured": 0})
        if math.isnan(level) or level < 0:
            b["unmeasured"] += 1
        else:
            b["sum"] += level
            b["count"] += 1
            if b["max"] is None or level > b["max"]:
                b["max"] = level

    if not buckets:
        return None

    return [
        {
            "hour": hour,
            "avgStress": (round(b["sum"] / b["count"]) if b["count"] else None),
            "maxStress": b["max"],
            "sampleCount": b["count"] + b["unmeasured"],
            "unmeasuredCount": b["unmeasured"],
        }
        for hour, b in sorted(buckets.items())
    ]


def trim_stress(stress: Any) -> Any:
    """Trim the standalone `get_stress` response.

    Reduces a ~30KB upstream stress payload to ~1KB by:
      - Dropping the 480-sample `stressValuesArray` and emitting a 24-entry
        `stressBuckets` aggregation (one per local-day hour) instead.
      - Dropping the bundled body-battery sub-payload (`bodyBatteryValuesArray`,
        descriptors, activity & dynamic-feedback events) — that data has
        its own dedicated `get_body_battery` tool.
      - Dropping chart-layout hints (`stressChartValueOffset`,
        `stressChartYAxisOrigin`), descriptor metadata, and `userProfilePK`.

    Every other top-level field passes through, so summary stats like
    `stressDuration` / `lowStressDuration` / `highStressDuration` survive.

    Non-dict inputs and error stubs (dicts with an `error` key — produced
    by the upstream `safe_call`) pass through unchanged so the diagnostic
    isn't swallowed by the trim.
    """
    if not isinstance(stress, dict):
        return stress
    if "error" in stress:
        return stress

    buckets = _compute_stress_buckets(
        stress.get("stressValuesArray"),
        stress.get("startTimestampGMT"),
        stress.get("endTimestampGMT"),
    )

    drop = _STRESS_DROP_ALWAYS | _STRESS_DROP_DEFAULT
    trimmed = {k: v for k, v in stress.items() if k not in drop}
    if buckets is not None:
        trimmed["stressBuckets"] = buckets
    return trimmed


# Top-level body-battery fields preserved by `trim_body_battery`. Anything
# else is dropped — including `userProfilePK`, `bodyBatteryVersion` (always
# 3, internal), `bodyBatteryValueDescriptorDTOList` (descriptor metadata
# for the values array — the array's first element is always a timestamp
# and second is always a level, so descriptors add no signal), and any
# hypothetical verbose secondary array key. The compressed transition
# `bodyBatteryValuesArray` (typically 6-12 entries of `[ts, level]`) IS
# kept — it's the high-signal summary the LLM uses to read drain/recharge
# inflection points across the day.
_BB_TOP_LEVEL_KEEP = frozenset(
    {
        "date",
        "charged",
        "drained",
        "startTimestampGMT",
        "startTimestampLocal",
        "endTimestampGMT",
        "endTimestampLocal",
        "bodyBatteryValuesArray",
        "bodyBatteryActivityEvent",
        "bodyBatteryDynamicFeedbackEvent",
        "endOfDayBodyBatteryDynamicFeedbackEvent",
    }
)

# Fields kept inside each entry of `bodyBatteryActivityEvent`. Each entry
# describes one sleep/exercise/recovery window and its body-battery impact;
# we keep the analytic fields (type, time, duration, impact, feedback,
# linked activity reference) and drop device/audit metadata that the LLM
# never reasons over.
_BB_ACTIVITY_EVENT_KEEP = frozenset(
    {
        "eventType",
        "eventStartTimeGmt",
        "durationInMilliseconds",
        "bodyBatteryImpact",
        "feedbackType",
        "shortFeedback",
        "activityName",
        "activityType",
        "activityId",
    }
)

# Fields kept inside each dynamic-feedback event (single-event objects on
# `bodyBatteryDynamicFeedbackEvent` and `endOfDayBodyBatteryDynamicFeedbackEvent`).
# `feedbackLongType` is kept: in some events it really is a verbose suffix
# of `feedbackShortType` (e.g. TYPICAL → WITHIN_TYPICAL_RANGE_FOR_THIS_TIME_OF_DAY),
# but in others it's a multi-tag composite like
# SLEEP_PREPARATION_STRESSFUL_AND_EXERCISE_AND_BB_LOW that captures
# narrative context not derivable from the kept shortType. Bytes saved
# would be one string per event (~2/day) — analytical value clearly wins.
_BB_DYNAMIC_FEEDBACK_KEEP = frozenset(
    {
        "eventTimestampGmt",
        "bodyBatteryLevel",
        "feedbackShortType",
        "feedbackLongType",
    }
)


def _trim_body_battery_entry(entry: Any) -> Any:
    """Trim a single per-day body-battery dict.

    Allowlist top level + sub-trim each `bodyBatteryActivityEvent` entry
    and the two dynamic-feedback event objects. Non-dict inputs and error
    stubs (dicts with an `error` key) pass through unchanged so list-level
    iteration keeps diagnostics.
    """
    if not isinstance(entry, dict):
        return entry
    if "error" in entry:
        return entry

    trimmed = {k: v for k, v in entry.items() if k in _BB_TOP_LEVEL_KEEP}

    activities = trimmed.get("bodyBatteryActivityEvent")
    if isinstance(activities, list):
        trimmed["bodyBatteryActivityEvent"] = [
            (
                {k: v for k, v in a.items() if k in _BB_ACTIVITY_EVENT_KEEP}
                if isinstance(a, dict)
                else a
            )
            for a in activities
        ]

    for key in ("bodyBatteryDynamicFeedbackEvent", "endOfDayBodyBatteryDynamicFeedbackEvent"):
        ev = trimmed.get(key)
        if isinstance(ev, dict):
            trimmed[key] = {k: v for k, v in ev.items() if k in _BB_DYNAMIC_FEEDBACK_KEEP}

    return trimmed


# Per-bucket fields dropped from every steps entry by `trim_steps`. `pushes`
# is always 0 for non-wheelchair users (when/if wheelchair mode lands we'll
# gate this behind a feature flag — out of scope here). `activityLevelConstant`
# is an algorithm-internal "was this bucket's activity level steady?" hint
# the LLM never reasons over.
_STEPS_DROP_PER_ENTRY = frozenset({"pushes", "activityLevelConstant"})


def trim_body_battery(payload: Any) -> Any:
    """Trim a `get_body_battery` response (multi-day list or single-day dict).

    Garmin's `bodyBattery/reports/daily` endpoint returns a list of dicts,
    one per day in the requested date range. Each per-day dict carries:

    - day summary (`date`, `charged`, `drained`, four start/end timestamps)
    - `bodyBatteryValuesArray` — compressed `[[ts, level], ...]` transition
      list (~6-12 entries, the inflection points)
    - `bodyBatteryActivityEvent` — list of activity-tied impact entries
      (sleep, exercise, recovery, etc. with bodyBatteryImpact)
    - `bodyBatteryDynamicFeedbackEvent` / `endOfDayBodyBatteryDynamicFeedbackEvent` —
      single-event objects with the level + short and long feedback codes

    All survive the trim. Algorithm internals (descriptor metadata,
    `bodyBatteryVersion`, `userProfilePK`) and per-event device/audit
    metadata (`deviceId`, `eventUpdateTimeGmt`, `timezoneOffset`) are
    dropped.

    Operates over a list (the normal Garmin response shape) by mapping
    over each entry. Single-dict inputs are also accepted defensively.
    Non-list/non-dict inputs and error stubs pass through unchanged so
    upstream diagnostics from `safe_call` aren't swallowed.
    """
    if isinstance(payload, list):
        return [_trim_body_battery_entry(entry) for entry in payload]
    return _trim_body_battery_entry(payload)


def _clean_steps_entry(entry: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in entry.items() if k not in _STEPS_DROP_PER_ENTRY}


def trim_steps(payload: Any) -> Any:
    """Trim a `get_steps` response (list of 96 fixed 15-min buckets).

    Garmin's `usersummary-service/stats/steps/daily/...` endpoint returns a
    bare list of 96 fixed-width 15-min buckets for the day. On a typical
    day ~70% of those buckets are zero-step (sleep + sedentary lulls), so
    we collapse contiguous zero-step runs that share the same
    `primaryActivityLevel` into a single bucket whose window spans the
    full run. Concretely the trim:

    - drops `pushes` from every entry (always 0 unless wheelchair mode —
      out of scope here; revisit behind a feature flag if/when supported)
    - drops `activityLevelConstant` from every entry (algorithm internal)
    - collapses contiguous runs of zero-step buckets that share the same
      `primaryActivityLevel` into one bucket spanning the run (start of
      first, end of last). Non-zero `steps` or an activity-level change
      ends a run — even a same-level zero-to-zero transition is preserved
      across an activity-level boundary (e.g. sleeping→sedentary at
      wake-up is information worth keeping).

    Non-zero buckets are never collapsed even if adjacent buckets share
    the same activity level — every signal where the user actually moved
    survives.

    Non-list inputs pass through unchanged. Inside a list, error stubs
    (dicts with an `error` key — produced by the upstream `safe_call`)
    and non-dict entries pass through unchanged so list-level iteration
    keeps diagnostics. Surviving entries keep every field other than the
    two dropped above, so a future Garmin field auto-passes-through; the
    test suite's allowlist canary catches additions.
    """
    if not isinstance(payload, list):
        return payload

    out: list[Any] = []
    run_start: dict[str, Any] | None = None
    run_end: dict[str, Any] | None = None
    run_level: Any = None

    def _flush() -> None:
        nonlocal run_start, run_end, run_level
        if run_start is None:
            return
        collapsed = _clean_steps_entry(run_start)
        if run_end is not None and run_end is not run_start:
            collapsed["endGMT"] = run_end.get("endGMT", collapsed.get("endGMT"))
        out.append(collapsed)
        run_start = None
        run_end = None
        run_level = None

    for entry in payload:
        if not isinstance(entry, dict) or "error" in entry:
            _flush()
            out.append(entry)
            continue

        steps = entry.get("steps")
        level = entry.get("primaryActivityLevel")
        # `isinstance(False, int)` is True in Python, so `False == 0` is also
        # True — without the bool reject a `False`-valued bucket would collapse
        # as a zero-step bucket. Matches the bool guard in `_compute_stress_buckets`.
        is_zero_run_candidate = steps == 0 and not isinstance(steps, bool)

        if is_zero_run_candidate and run_start is not None and level == run_level:
            run_end = entry
            continue

        _flush()

        if is_zero_run_candidate:
            run_start = entry
            run_end = entry
            run_level = level
        else:
            out.append(_clean_steps_entry(entry))

    _flush()
    return out
