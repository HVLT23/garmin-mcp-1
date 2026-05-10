"""Payload trimmers for sleep/HRV/stress responses.

Shared between the standalone wellness tools (`get_sleep`, `get_hrv`,
`get_stress`) and the aggregate `get_session_with_context` tool. Two
strategies are exposed for sleep because the two contexts have different
needs:

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

All strategies treat non-dict input (e.g. error stubs) as a pass-through.
"""

from __future__ import annotations

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


def _compute_stress_buckets(samples: Any, start_ts: Any) -> list[dict[str, Any]] | None:
    """Aggregate 3-min stress samples into one bucket per local-day hour.

    Sample timestamps and `start_ts` must be in the same encoding (both
    GMT-ms or both local-ms-encoded-as-utc — Garmin returns parallel
    `startTimestampGMT` / `startTimestampLocal` fields and the deltas
    between them and the sample timestamps are identical, so the chosen
    encoding doesn't change the resulting hour index).

    Returns `None` if the inputs are unusable. Otherwise emits a list of
    dicts, one per hour that has at least one sample, sorted by hour.
    Hours with zero samples are omitted (per spec).

    Garmin marks unmeasured samples with `-1` (no recent HR) or `-2` (no
    contact). They're counted in `unmeasuredCount` but excluded from
    `avgStress` / `maxStress`. A bucket whose every sample is unmeasured
    yields `avgStress: None`, `maxStress: None`.
    """
    if not isinstance(samples, list) or not samples:
        return None
    if not isinstance(start_ts, int):
        return None

    buckets: dict[int, dict[str, Any]] = {}
    for entry in samples:
        if not isinstance(entry, list) or len(entry) < 2:
            continue
        ts, level = entry[0], entry[1]
        if not isinstance(ts, (int, float)):
            continue
        if not isinstance(level, (int, float)):
            continue
        hour = int((ts - start_ts) // _HOUR_MS)
        if hour < 0 or hour >= 24:
            # Sample falls outside the day window — likely a daylight-saving
            # boundary. Skip rather than corrupt the bucket layout.
            continue
        b = buckets.setdefault(hour, {"sum": 0, "count": 0, "max": None, "unmeasured": 0})
        if level < 0:
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
    )

    drop = _STRESS_DROP_ALWAYS | _STRESS_DROP_DEFAULT
    trimmed = {k: v for k, v in stress.items() if k not in drop}
    if buckets is not None:
        trimmed["stressBuckets"] = buckets
    return trimmed
