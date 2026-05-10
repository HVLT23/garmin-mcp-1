"""Payload trimmers for sleep/HRV responses.

Shared between the standalone `get_sleep`/`get_hrv` tools and the aggregate
`get_session_with_context` tool. Two strategies are exposed because the two
contexts have different needs:

- `trim_sleep_streams` — drops only the per-minute / per-5-min streams from a
  sleep response. Used by the aggregate tool, which wants the full sleep
  summary alongside an activity for cross-context analysis.
- `trim_sleep` — aggressive allowlist trim for the standalone `get_sleep`
  tool. The standalone tool is asked specifically for sleep data, so we can
  drop algorithm-internal fields (sleepWindowConfirmed, sleepFromDevice,
  ageGroup, …) and keep only the analytically useful summary.

Both strategies treat non-dict input (e.g. error stubs) as a pass-through.
"""

from __future__ import annotations

from typing import Any

# Per-minute / per-5-min stream fields dropped by the aggregate tool's
# lite trim. These five account for ~80% of payload size but no analytical
# use case consumes them. The standalone `get_sleep` trim drops more via
# its top-level allowlist (see `_SLEEP_TOP_LEVEL_KEEP`).
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
        stages, scores, score insights, sleep need).
      - Keeping `sleepScores.overall` and dropping the algorithm normal
        ranges (idealStartInSeconds, optimalStart, …).
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
            overall = scores.get("overall")
            if isinstance(overall, dict):
                filtered["sleepScores"] = {
                    "overall": {
                        k: v for k, v in overall.items() if k not in _SLEEP_SCORE_DROP
                    }
                }
            elif overall is not None:
                filtered["sleepScores"] = {"overall": overall}
            else:
                filtered.pop("sleepScores", None)
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
