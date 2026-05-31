"""Payload trimmers for sleep/HRV/stress/body-battery/steps/activity responses.

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

# Cross-cutting drops applied as a final pass by every trim helper via
# `_strip_pii`. Three categories:
#
# 1. User-identity fields. Garmin returns these at multiple nesting levels
#    (`userProfilePK` at the top of a stress response, `ownerId` /
#    `ownerDisplayName` / `ownerFullName` / `ownerProfileImageUrl*` on each
#    activity-summary item, `userInfoDto.displayName` / `fullName` /
#    `profileImageUrl*` inside `activity.metadataDTO`, …). In an
#    authenticated MCP context there is exactly one user and the LLM never
#    reasons over their own identifiers — drop everywhere.
# 2. Image URLs (`imageURL` / `imageUrl`). Appear in `recordedDevices` blocks
#    (training_status / training_load when those tools are trimmed in a
#    future PR) and in profile-image fields. Always unactionable for the LLM.
# 3. `userPro` (boolean subscription flag). Mostly-default, never load-bearing.
#
# `displayname` / `displayName` / `fullname` / `fullName` cover both
# camelCase and lowercase variants Garmin sometimes returns in the same
# response (e.g. `ownerDisplayName` is camelCase but other endpoints emit
# `displayname` lowercase). Cheap to include both.
_PII_KEYS = frozenset(
    {
        # User identity
        "userProfileId",
        "userId",
        "userProfilePK",
        "userProfilePk",
        "displayname",
        "displayName",
        "fullname",
        "fullName",
        "profileImageUrlSmall",
        "profileImageUrlMedium",
        "profileImageUrlLarge",
        "ownerDisplayName",
        "ownerFullName",
        "ownerId",
        "ownerProfileImageUrlSmall",
        "ownerProfileImageUrlMedium",
        "ownerProfileImageUrlLarge",
        "userPro",
        # Image URLs (any nesting level — `recordedDevices[].imageURL`, etc.)
        "imageURL",
        "imageUrl",
    }
)

# Local-timestamp suffixes. Garmin returns parallel `*Gmt`/`*GMT` and
# `*Local` copies of every timestamp (e.g. `startTimestampGMT` vs
# `startTimestampLocal`, `wellnessStartTimeGmt` vs `wellnessStartTimeLocal`,
# `readingTimeGmt` vs `readingTimeLocal`). The `*Local` copy is the same
# wall-clock instant expressed in the user's timezone — given the GMT copy
# is always present, the LLM can derive local from GMT + offset if needed,
# so the `*Local` parallel is byte-for-byte redundant (~5-10% per payload).
#
# We restrict to the two suffix shapes Garmin actually emits (`TimeLocal`
# and `TimestampLocal`) rather than a bare `Local` suffix to avoid
# accidentally swallowing a future non-timestamp `someLocal`-named field.
# Matching is case-insensitive so lowercase parallels like `timestampLocal`
# (training_readiness emits this bare-key form alongside `timestamp`) drop
# without per-tool workarounds. `calendarDate` and other date-only fields
# (no time component) carry no `Local` suffix and are unaffected.
_LOCAL_TIMESTAMP_SUFFIXES = ("timelocal", "timestamplocal")


def _is_local_timestamp_key(key: Any) -> bool:
    if not isinstance(key, str):
        return False
    lower = key.lower()
    return any(lower.endswith(suffix) for suffix in _LOCAL_TIMESTAMP_SUFFIXES)


def _strip_pii(obj: Any) -> Any:
    """Recursively drop PII / image-URL / `*Local`-timestamp duplicates.

    Applied as the final pass of every per-tool trimmer so the cross-cutting
    drops live in one place. Per-tool denylists / allowlists handle
    tool-specific noise; this helper handles the patterns that recur
    across every tool (user identity, image URLs, redundant `*Local`
    timestamps).

    Error stubs (dicts with an `error` key — produced by upstream
    `safe_call`) pass through unchanged so the diagnostic isn't swallowed.
    Non-dict / non-list inputs (None, str, int, …) pass through unchanged.
    """
    if isinstance(obj, dict):
        if "error" in obj:
            return obj
        return {
            k: _strip_pii(v)
            for k, v in obj.items()
            if k not in _PII_KEYS and not _is_local_timestamp_key(k)
        }
    if isinstance(obj, list):
        return [_strip_pii(item) for item in obj]
    return obj


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
    return _strip_pii(trimmed)


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

    return _strip_pii(trimmed)


_HRV_DROP = frozenset({"hrvReadings", "userProfilePk"})


def trim_hrv(hrv: Any) -> Any:
    """Strip the per-5-min `hrvReadings` array (and `userProfilePk`) from an HRV response.

    Keeps the summary block (lastNightAvg, weeklyAvg, status, feedbackPhrase,
    baseline…) and the surrounding sleep-window timestamps. `userProfilePk`
    is dropped for parity with `trim_stress` / `trim_body_battery`, which
    already strip the equivalent identifier; the aggregate tool consumes
    the same trim and didn't carry the field in its expected output.
    Non-dict inputs pass through unchanged.
    """
    if not isinstance(hrv, dict):
        return hrv
    return _strip_pii({k: v for k, v in hrv.items() if k not in _HRV_DROP})


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
    return _strip_pii(trimmed)


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
        return [_strip_pii(_trim_body_battery_entry(entry)) for entry in payload]
    return _strip_pii(_trim_body_battery_entry(payload))


def _clean_steps_entry(entry: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in entry.items() if k not in _STEPS_DROP_PER_ENTRY}


# Always-drop noise on each per-activity summary item returned by
# `list_recent_activities` and `search_activities_by_type`. Three buckets:
#
# 1. PII / OAuth / image URLs (~80% of the bloat on a summary item):
#    `userRoles` is a ~30-entry OAuth scope list duplicated on every
#    activity; `ownerId` / `ownerDisplayName` / `ownerFullName` are
#    redundant for an authenticated session; `ownerProfileImageUrl{Small,
#    Medium,Large}` add ~600 bytes of S3 URLs per item.
# 2. Constant-value / mostly-default booleans the LLM never reasons over:
#    `userPro`, `hasVideo`, `hasImages`, `hasHeatMap`, `hasIntensityIntervals`,
#    `hasSplits`, `hasPolyline` (UI hints); `manufacturer` (always GARMIN),
#    `parent`, `decoDive`, `qualifyingDive` (always false unless dive
#    activity), `atpActivity`, `manualActivity`, `purposeful`, `favorite`,
#    `pr`, `autoCalcCalories`, `elevationCorrected` (all-default booleans).
# 3. Redundancy / internal identifiers:
#    `privacy` (always `{typeId:3, typeKey:"subscribers"}` for personal use),
#    `timeZoneId` (numeric, redundant with start/end timestamps that already
#    encode offsets), `activityUUID` (`activityId` is the canonical
#    reference; the UUID is the Garmin-internal alternate key).
#
# `summarizedDiveInfo` (always `{summarizedDiveGases: []}` for non-dive
# activities) and empty `splitSummaries: []` (one allocation per summary
# item) are dropped conditionally below — empty wrappers carry no signal
# but populated ones are kept.
_ACTIVITY_SUMMARY_DROP_ALWAYS = frozenset(
    {
        # PII / OAuth / image URLs
        "userRoles",
        "ownerId",
        "ownerDisplayName",
        "ownerFullName",
        "ownerProfileImageUrlSmall",
        "ownerProfileImageUrlMedium",
        "ownerProfileImageUrlLarge",
        # UI hints + privacy
        "privacy",
        "userPro",
        "hasVideo",
        "hasImages",
        "hasHeatMap",
        "hasIntensityIntervals",
        "hasSplits",
        "hasPolyline",
        # Default-valued booleans
        "manufacturer",
        "parent",
        "decoDive",
        "qualifyingDive",
        "atpActivity",
        "manualActivity",
        "purposeful",
        "favorite",
        "pr",
        "autoCalcCalories",
        "elevationCorrected",
        # Redundancy / internal identifiers
        "timeZoneId",
        "activityUUID",
        "beginTimestamp",  # Unix-ms duplicate of startTimeGMT
        "sportTypeId",  # numeric internal; activityType.typeId is canonical
    }
)


def _trim_activity_summary_item(item: Any) -> Any:
    """Trim one summary item from `list_recent_activities` / `search_activities_by_type`.

    Non-dict inputs and error stubs (`{"error": ...}` from `safe_call`) pass
    through unchanged so list-level iteration keeps diagnostics. Surviving
    fields are everything outside `_ACTIVITY_SUMMARY_DROP_ALWAYS`, plus
    drops for the conditional cases below (empty `summarizedDiveInfo`
    wrapper, empty `splitSummaries` list).
    """
    if not isinstance(item, dict):
        return item
    if "error" in item:
        return item

    trimmed = {k: v for k, v in item.items() if k not in _ACTIVITY_SUMMARY_DROP_ALWAYS}

    # `summarizedDiveInfo` is always `{"summarizedDiveGases": []}` for
    # non-dive activities. Drop when it carries no actual dive data — the
    # `qualifyingDive` flag stays in the always-drop set, but a dive
    # activity would have a populated `summarizedDiveGases` list.
    dive = trimmed.get("summarizedDiveInfo")
    if isinstance(dive, dict):
        gases = dive.get("summarizedDiveGases")
        if not gases:  # None or empty list
            del trimmed["summarizedDiveInfo"]

    # `splitSummaries` is `[]` for non-segmented activities (strength,
    # boxing, indoor sports). Drop when empty; keep populated lists since
    # they hold per-segment aggregates (run/walk/stand splits etc.).
    splits = trimmed.get("splitSummaries")
    if isinstance(splits, list) and not splits:
        del trimmed["splitSummaries"]

    return trimmed


def trim_activity_summary(payload: Any) -> Any:
    """Trim a list of activity-summary items (or a single item, defensively).

    Used by both `list_recent_activities` and `search_activities_by_type` —
    they share the per-activity summary shape. Maps `_trim_activity_summary_item`
    over each entry. Non-list / non-dict inputs pass through unchanged so
    upstream diagnostics from `safe_call` aren't swallowed.

    Measured ~2x reduction on Kamil's recent activities (5-item
    `list_recent_activities` response: 19213 → 8609 chars, 2.23x;
    3-item `search_activities_by_type` for running: 17082 → 10795 chars,
    1.58x — running activities carry more legitimate signal like lat/lng,
    elevation, power zones, and populated `splitSummaries`, which pulls
    the ratio down). Drops are dominated by the `userRoles` OAuth scope
    list and the three S3 profile-image URLs duplicated on every item.
    """
    if isinstance(payload, list):
        return [_strip_pii(_trim_activity_summary_item(item)) for item in payload]
    return _strip_pii(_trim_activity_summary_item(payload))


# Top-level fields kept by `trim_activity_detail`. The live `get_activity`
# response is a small dict (11 keys); allowlist is more honest than denylist
# here. Dropped: `accessControlRuleDTO` (privacy stub),
# `activityUUID` (redundant with `activityId`), `isMultiSportParent`
# (default false), `userProfileId` (PII for authenticated session).
_ACTIVITY_DETAIL_TOP_LEVEL_KEEP = frozenset(
    {
        "activityId",
        "activityName",
        "activityTypeDTO",
        "eventTypeDTO",
        "summaryDTO",
        "timeZoneUnitDTO",
        "metadataDTO",
    }
)

# Fields kept inside `metadataDTO` unconditionally. The block is almost
# entirely device / audit metadata + a nested PII block (`userInfoDto`
# with image URLs); we keep only the small set of fields with downstream
# analytical value: `lapCount` (helps cross-reference the splits call),
# and the two upload / update timestamps (data freshness signal).
# Everything else (`deviceMetaDataDTO`, `userInfoDto`, the
# `has{Polyline,HeatMap,…}` UI hints, `isOriginal` / `trimmed` /
# `personalRecord` / `gcj02` booleans, `manufacturer`, `childIds`,
# `agentString`, `videoUrl`, etc.) is dropped.
_ACTIVITY_DETAIL_METADATA_KEEP = frozenset(
    {
        "lapCount",
        "lastUpdateDate",
        "uploadedDate",
    }
)

# Fields kept inside `metadataDTO` *only when populated* (non-null /
# non-empty). On a non-eBike / non-templated / sensor-less activity
# every value is null, and unconditional pass-through would add ~80
# bytes of `null` noise per activity. Conditional keep gives us
# real telemetry when it exists without the per-activity null tax:
#
# - `eBike{BatteryUsage,BatteryRemaining,MaxAssistModes,AssistModeInfoDTOList}`
#   are populated only for eBike rides. On a real eBike activity the
#   LLM reasons over "how much battery did I burn / which assist modes
#   did I cycle through?" — that's genuine telemetry, not bloat.
# - `associatedWorkoutId` is set when the activity was driven by a
#   Garmin Connect workout template ("did the user follow a planned
#   workout?").
# - `associatedCourseId` is set when the activity ran a Connect course
#   ("did the user run a planned route?").
# - `sensors` is the connected external-sensor list (HR strap, power
#   meter, cadence sensor, …) when paired sensors were used; the LLM
#   may use it to weight HR/power confidence.
_ACTIVITY_DETAIL_METADATA_CONDITIONAL_KEEP = (
    "eBikeBatteryUsage",
    "eBikeBatteryRemaining",
    "eBikeMaxAssistModes",
    "eBikeAssistModeInfoDTOList",
    "associatedWorkoutId",
    "associatedCourseId",
    "sensors",
)


def _is_meaningful(value: Any) -> bool:
    """True if `value` is non-null and not an empty container.

    `False` / `0` count as meaningful (a 0% battery reading is still
    telemetry). Only `None`, `[]`, `{}`, `""` are treated as absent.
    """
    if value is None:
        return False
    return not (isinstance(value, (list, dict, str)) and not value)


def trim_activity_detail(activity: Any) -> Any:
    """Trim a `get_activity` response (single activity-detail dict).

    Reduces a ~3KB upstream `get_activity` payload by ~2-3x by:
      - Allowlisting top-level keys to the small set with analytical value
        (`activityId`, `activityName`, `activityTypeDTO`, `eventTypeDTO`,
        `summaryDTO`, `timeZoneUnitDTO`, `metadataDTO`).
      - Allowlisting `metadataDTO` down to `lapCount` and the two
        upload/update timestamps — the rest of the block is device /
        audit metadata + a PII sub-block (`userInfoDto`). Populated
        eBike telemetry (`eBike{BatteryUsage,BatteryRemaining,…}`),
        `associatedWorkoutId` / `associatedCourseId` (Connect workout /
        course references), and a non-empty `sensors` list pass through
        when present — they're real signal on the activities that have
        them, and null/empty on the common case so the per-activity null
        tax is avoided.
      - Dropping `accessControlRuleDTO`, `activityUUID`, `isMultiSportParent`,
        `userProfileId` at top level.

    `summaryDTO` (the analytically dense block: durations, distance,
    calories, HR, training effect, sport-specific stats) passes through
    untouched.

    Non-dict inputs and error stubs (dicts with an `error` key — produced
    by the upstream `safe_call`) pass through unchanged so the diagnostic
    isn't swallowed by the allowlist trim.
    """
    if not isinstance(activity, dict):
        return activity
    if "error" in activity:
        return activity

    trimmed = {k: v for k, v in activity.items() if k in _ACTIVITY_DETAIL_TOP_LEVEL_KEEP}

    meta = trimmed.get("metadataDTO")
    if isinstance(meta, dict):
        filtered = {k: v for k, v in meta.items() if k in _ACTIVITY_DETAIL_METADATA_KEEP}
        # Conditional pass-through: keep populated eBike telemetry,
        # workout/course associations, and connected-sensor list. On a
        # null/empty value the field is dropped (the common case for
        # non-eBike / un-templated activities).
        for k in _ACTIVITY_DETAIL_METADATA_CONDITIONAL_KEEP:
            v = meta.get(k)
            if _is_meaningful(v):
                filtered[k] = v
        trimmed["metadataDTO"] = filtered

    return _strip_pii(trimmed)


# Per-lap fields dropped from each `lapDTOs` entry when empty. Both fields
# are populated only for specific activity types (swim lengths → `lengthDTOs`,
# Connect IQ apps writing to FIT laps → `connectIQMeasurement`); on a
# typical run / ride / strength session both are `[]`. Drop only when
# empty so swimming / Connect IQ data still flows through.
_ACTIVITY_SPLIT_LAP_DROP_IF_EMPTY = frozenset(
    {
        "lengthDTOs",
        "connectIQMeasurement",
    }
)


def trim_activity_splits(splits: Any) -> Any:
    """Trim a `get_activity_splits` response (`{activityId, lapDTOs, eventDTOs}`).

    Drops `eventDTOs` entirely — every entry is a `TIMER_TRIGGER` /
    `LAP_TRIGGER` marker for auto-pause / manual-lap presses on the watch.
    The LLM never reasons over them, and a strength session typically
    carries two such markers per lap (a 1-lap strength session yielded
    2 events at ~330 bytes; a multi-lap run scales linearly).

    For each `lapDTOs` entry: drop `lengthDTOs` and `connectIQMeasurement`
    if they're empty lists. Both are populated only for swim / Connect-IQ
    activities — dropping when empty saves bytes on the common case
    without disturbing the data-bearing case.

    Non-dict inputs and error stubs pass through unchanged.
    """
    if not isinstance(splits, dict):
        return splits
    if "error" in splits:
        return splits

    trimmed = {k: v for k, v in splits.items() if k != "eventDTOs"}

    laps = trimmed.get("lapDTOs")
    if isinstance(laps, list):
        trimmed["lapDTOs"] = [_trim_split_lap(lap) for lap in laps]

    return _strip_pii(trimmed)


def _trim_split_lap(lap: Any) -> Any:
    if not isinstance(lap, dict):
        return lap
    return {
        k: v
        for k, v in lap.items()
        if not (k in _ACTIVITY_SPLIT_LAP_DROP_IF_EMPTY and isinstance(v, list) and not v)
    }


# Cross-tool noise + identifiers stripped from `get_daily_summary` regardless
# of value. Three buckets:
#
# 1. PII / identifiers (parity with `trim_stress` / `trim_body_battery`):
#    `userProfileId`, `userDailySummaryId`, `uuid`. The latter two are
#    Garmin-internal alternate keys with no analytical use.
# 2. Privacy / source / version / sync metadata: `rule` (always
#    `{typeId:3, typeKey:"subscribers"}`), `privacyProtected` (always false),
#    `source` (always `"GARMIN"`), `bodyBatteryVersion` /
#    `respirationAlgorithmVersion` (algorithm internals), `wellnessDescription`
#    (always null), `lastSyncTimestampGMT` (device-sync metadata).
# 3. UI / availability hints: `includesWellnessData`, `includesActivityData`,
#    `includesCalorieConsumedData`.
# 4. Wellness aliases: `wellnessKilocalories`, `wellnessActiveKilocalories`,
#    `wellnessDistanceMeters` are byte-for-byte equal to `totalKilocalories`
#    / `activeKilocalories` / `totalDistanceMeters` on every payload we've
#    seen — keep the non-`wellness*` copy, drop the alias.
# 5. Cross-tool body-battery duplicates: `bodyBatteryActivityEventList`,
#    `bodyBatteryDynamicFeedbackEvent`, `endOfDayBodyBatteryDynamicFeedbackEvent`
#    are also returned (in equivalent shape) by `get_body_battery`. Drop
#    the daily-summary copies; the dedicated body-battery tool is the
#    single source of truth for this data.
_DAILY_SUMMARY_DROP_ALWAYS = frozenset(
    {
        # PII / identifiers
        "userProfileId",
        "userDailySummaryId",
        "uuid",
        # Privacy / source / version / sync metadata
        "rule",
        "privacyProtected",
        "source",
        "bodyBatteryVersion",
        "respirationAlgorithmVersion",
        "wellnessDescription",
        "lastSyncTimestampGMT",
        # UI / availability hints
        "includesWellnessData",
        "includesActivityData",
        "includesCalorieConsumedData",
        # Wellness aliases (duplicate of non-wellness fields)
        "wellnessKilocalories",
        "wellnessActiveKilocalories",
        "wellnessDistanceMeters",
        # Cross-tool body-battery duplicates (covered by `get_body_battery`)
        "bodyBatteryActivityEventList",
        "bodyBatteryDynamicFeedbackEvent",
        "endOfDayBodyBatteryDynamicFeedbackEvent",
    }
)

# Spo2 fields dropped together when no Spo2 reading was captured. On a
# wrist-worn device without overnight Spo2 enabled (or during a day with
# no measurement window) all three readings are null and the two
# timestamp fields are null too — five `null` keys per day. When ANY
# reading is non-null the whole block survives. Mirrors the conditional
# eBike-telemetry pass-through in `trim_activity_detail`.
_DAILY_SUMMARY_SPO2_FIELDS = frozenset(
    {
        "averageSpo2",
        "lowestSpo2",
        "latestSpo2",
        "latestSpo2ReadingTimeGmt",
        "latestSpo2ReadingTimeLocal",
    }
)


def trim_daily_summary(summary: Any) -> Any:
    """Trim a `get_daily_summary` response.

    Reduces a ~4.5KB upstream payload by ~1.4-1.5x by dropping three
    categories of noise:

      - Cross-tool duplicates that have a dedicated tool:
        `bodyBatteryActivityEventList`, `bodyBatteryDynamicFeedbackEvent`,
        `endOfDayBodyBatteryDynamicFeedbackEvent` (all in `get_body_battery`).
      - Wellness aliases: `wellnessKilocalories`, `wellnessActiveKilocalories`,
        `wellnessDistanceMeters` (byte-equal to the non-`wellness*` copies).
      - PII / identifiers / version / privacy / sync metadata:
        `userProfileId`, `userDailySummaryId`, `uuid`, `rule`,
        `privacyProtected`, `source`, `bodyBatteryVersion`,
        `respirationAlgorithmVersion`, `wellnessDescription`,
        `lastSyncTimestampGMT`, `includes{Wellness,Activity,CalorieConsumed}Data`.

    Conditional drops:
      - All Spo2 fields (`averageSpo2`, `lowestSpo2`, `latestSpo2`,
        `latestSpo2ReadingTimeGmt`, `latestSpo2ReadingTimeLocal`) drop
        together when every reading is null. When any reading is non-null
        the whole block survives — mirrors the populated-eBike-telemetry
        pass-through in `trim_activity_detail`.
      - `abnormalHeartRateAlertsCount` drops when null (the common case);
        a non-null value is a medical signal and survives.

    Every other field passes through, so the daily-summary numbers
    (calories, steps, intensity minutes, RHR, stress percentages,
    body-battery summary values, respiration summary, …) are untouched.

    Non-dict inputs and error stubs (dicts with an `error` key — produced
    by the upstream `safe_call`) pass through unchanged so the diagnostic
    isn't swallowed by the trim.
    """
    if not isinstance(summary, dict):
        return summary
    if "error" in summary:
        return summary

    drop = set(_DAILY_SUMMARY_DROP_ALWAYS)

    # Conditional Spo2 drop: only suppress when *no* reading exists. Any
    # non-null value flips the whole block back on — including the two
    # timestamp fields, which are only meaningful alongside a reading.
    if all(summary.get(k) is None for k in ("averageSpo2", "lowestSpo2", "latestSpo2")):
        drop |= _DAILY_SUMMARY_SPO2_FIELDS

    # `abnormalHeartRateAlertsCount` is null on a normal day and worth
    # keeping when populated (medical signal). Drop only on null.
    if summary.get("abnormalHeartRateAlertsCount") is None:
        drop = drop | {"abnormalHeartRateAlertsCount"}

    return _strip_pii({k: v for k, v in summary.items() if k not in drop})


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
    return _strip_pii(out)


# ---------------------------------------------------------------------------
# Training-tools trimmers (training_status / training_load / training_readiness
# / vo2_max). All four tools share the same upstream `get_training_status`
# response shape — see `trim_training_status` for the full denylist
# rationale; the load/vo2 helpers reuse the same nested cleanup but extract
# only the sub-block relevant to their tool.
# ---------------------------------------------------------------------------

# Top-level fields dropped from the training-status payload only when null.
# `userId` drops at any value via the cross-cutting `_strip_pii`;
# `heatAltitudeAcclimationDTO` at the top level is always null on the data
# we've seen (the populated copy lives at
# `mostRecentVO2Max.heatAltitudeAcclimation`), but if Garmin ever moves
# real data back to the top level we should let it through rather than
# silently swallow it.
_TRAINING_STATUS_TOP_LEVEL_DROP_IF_NULL = frozenset(
    {
        "heatAltitudeAcclimationDTO",
    }
)

# Fields dropped from `mostRecentTrainingStatus` regardless of value.
# `recordedDevices` is a cross-tool repeat (the LLM already has the device
# on every other tool's payload), `showSelector` is a UI hint.
# `lastPrimarySyncDate` survives — it's a useful "data freshness" signal.
_TRAINING_STATUS_BLOCK_DROP = frozenset(
    {
        "recordedDevices",
        "showSelector",
    }
)

# Per-device fields dropped only when null. These four are perpetually null
# on the live payload (the populated copies live in
# `mostRecentTrainingLoadBalance`), but a future Garmin shape change that
# relocates real data here must not be silently swallowed — so drop only
# on null and let any non-null value pass through.
_TRAINING_STATUS_DEVICE_DROP_IF_NULL = frozenset(
    {
        "weeklyTrainingLoad",
        "loadTunnelMin",
        "loadTunnelMax",
        "loadLevelTrend",
    }
)

# Per-device fields dropped regardless of value. `deviceId` is redundant
# with the surrounding map key (or context after a single-device collapse).
# `primaryTrainingDevice` is intentionally NOT here — it carries signal on
# multi-device users (`True` on the primary, `False` on secondaries), so
# the collapse helper drops it only when collapsing to a single device
# (where it's always `True` and therefore noise).
_TRAINING_STATUS_DEVICE_DROP_ALWAYS = frozenset(
    {
        "deviceId",
    }
)

# Per-device fields dropped on every `metricsTrainingLoadBalanceDTOMap`
# entry regardless of value. Same `deviceId` redundancy as the
# training-status sibling. `primaryTrainingDevice` is again handled
# conditionally by the collapse helper.
_TRAINING_LOAD_DEVICE_DROP_ALWAYS = frozenset(
    {
        "deviceId",
    }
)


def _collapse_device_map(
    value: Any,
    per_device_drop_always: frozenset[str],
    per_device_drop_if_null: frozenset[str] = frozenset(),
) -> Any:
    """Trim each device entry; collapse `{deviceId: {...}}` to the inner dict
    when there's exactly one device.

    Garmin nests device-keyed `{<deviceId>: {...}}` maps in two places
    (`mostRecentTrainingStatus.latestTrainingStatusData` and
    `mostRecentTrainingLoadBalance.metricsTrainingLoadBalanceDTOMap`).
    On a single-device user (the common case) the outer key is meaningless
    indirection — collapse to the inner dict so the LLM doesn't have to
    pivot through an unknown numeric ID. On a multi-device user the map is
    preserved so the LLM can still distinguish the watches.

    Each per-device sub-dict has `per_device_drop_always` keys removed
    unconditionally and `per_device_drop_if_null` keys removed only when
    null. The conditional-null path lets a future Garmin shape change
    that relocates real data into one of those keys flow through rather
    than be silently swallowed.

    `primaryTrainingDevice` is also dropped, but only on the single-device
    collapse path — on a single watch it's always `True` and therefore
    noise; on multi-device it's the only signal distinguishing primary
    from secondaries (the surviving map key is opaque).

    Non-dict inputs pass through unchanged so a missing map (already-null
    upstream) doesn't crash.
    """
    if not isinstance(value, dict):
        return value

    def _clean(entry: Any) -> Any:
        if not isinstance(entry, dict):
            return entry
        return {
            k: v
            for k, v in entry.items()
            if k not in per_device_drop_always
            and not (k in per_device_drop_if_null and v is None)
        }

    cleaned = {device_id: _clean(entry) for device_id, entry in value.items()}
    if len(cleaned) == 1:
        only = next(iter(cleaned.values()))
        # On the single-device collapse path `primaryTrainingDevice` is
        # always `True` and therefore noise; drop it. On multi-device
        # (the else branch) we preserve it so the LLM can tell which
        # watch is primary.
        if isinstance(only, dict):
            only = {k: v for k, v in only.items() if k != "primaryTrainingDevice"}
        return only
    return cleaned


def trim_training_status(payload: Any) -> Any:
    """Trim a `get_training_status` response.

    Drops unconditionally:
      - `mostRecentTrainingStatus.recordedDevices` (cross-tool repeat) and
        `.showSelector` (UI hint).
      - Per-device `deviceId` (redundant with map key / surrounding context).
      - PII / `*Local` / image URLs at any depth via `_strip_pii`.

    Drops only when null (so a future Garmin shape change that populates
    these fields here flows through rather than being silently swallowed):
      - Top-level `heatAltitudeAcclimationDTO` (always null on current
        data; the populated copy lives at
        `mostRecentVO2Max.heatAltitudeAcclimation`).
      - Per-device `weeklyTrainingLoad`, `loadTunnelMin`, `loadTunnelMax`,
        `loadLevelTrend` (always null at this layer; populated copies live
        in `mostRecentTrainingLoadBalance`).

    Drops only on the single-device collapse path:
      - Per-device `primaryTrainingDevice` (always `True` on a single
        watch; on multi-device it's the only signal distinguishing
        primary from secondary, so it survives there).

    Collapses single-device `{<deviceId>: {...}}` maps under
    `latestTrainingStatusData` and `metricsTrainingLoadBalanceDTOMap` to
    the inner dict — the outer numeric-ID indirection is meaningless on a
    one-watch user. Multi-device maps are preserved.

    Non-dict inputs and error stubs pass through unchanged.
    """
    if not isinstance(payload, dict):
        return payload
    if "error" in payload:
        return payload

    trimmed: dict[str, Any] = {
        k: v
        for k, v in payload.items()
        if not (k in _TRAINING_STATUS_TOP_LEVEL_DROP_IF_NULL and v is None)
    }

    status_block = trimmed.get("mostRecentTrainingStatus")
    if isinstance(status_block, dict):
        new_block: dict[str, Any] = {
            k: v for k, v in status_block.items() if k not in _TRAINING_STATUS_BLOCK_DROP
        }
        latest = new_block.get("latestTrainingStatusData")
        if isinstance(latest, dict):
            new_block["latestTrainingStatusData"] = _collapse_device_map(
                latest,
                _TRAINING_STATUS_DEVICE_DROP_ALWAYS,
                _TRAINING_STATUS_DEVICE_DROP_IF_NULL,
            )
        trimmed["mostRecentTrainingStatus"] = new_block

    load_block = trimmed.get("mostRecentTrainingLoadBalance")
    if isinstance(load_block, dict):
        new_load = dict(load_block)
        per_device = new_load.get("metricsTrainingLoadBalanceDTOMap")
        if isinstance(per_device, dict):
            new_load["metricsTrainingLoadBalanceDTOMap"] = _collapse_device_map(
                per_device, _TRAINING_LOAD_DEVICE_DROP_ALWAYS
            )
        trimmed["mostRecentTrainingLoadBalance"] = new_load

    return _strip_pii(trimmed)


def trim_training_load(payload: Any) -> Any:
    """Trim a `get_training_load` response down to load-balance fields only.

    The upstream `get_training_status` payload contains
    `mostRecentTrainingLoadBalance` alongside `mostRecentTrainingStatus`
    and `mostRecentVO2Max`. The load tool re-uses the same upstream
    fetcher (and cache) but scopes its response to the load-balance block
    only — users wanting status or VO2 max should call the dedicated
    tools rather than receive a 95%-duplicate copy here.

    Keeps:
      - `mostRecentTrainingLoadBalance` only, with the device-map collapse
        applied. `deviceId` drops on every entry; `primaryTrainingDevice`
        drops only on the single-device collapse path (signal-bearing
        on multi-device users).

    Drops:
      - `mostRecentTrainingStatus`, `mostRecentVO2Max`,
        `heatAltitudeAcclimationDTO`, `userId` (and any other top-level
        sibling).
      - PII / `*Local` / image URLs at any depth via `_strip_pii`.

    Non-dict inputs and error stubs pass through unchanged.
    """
    if not isinstance(payload, dict):
        return payload
    if "error" in payload:
        return payload

    load_block = payload.get("mostRecentTrainingLoadBalance")
    if not isinstance(load_block, dict):
        return {}

    new_load = dict(load_block)
    per_device = new_load.get("metricsTrainingLoadBalanceDTOMap")
    if isinstance(per_device, dict):
        new_load["metricsTrainingLoadBalanceDTOMap"] = _collapse_device_map(
            per_device, _TRAINING_LOAD_DEVICE_DROP_ALWAYS
        )

    return _strip_pii({"mostRecentTrainingLoadBalance": new_load})


# Always-drop noise from a training-readiness reading.
_TRAINING_READINESS_DROP = frozenset(
    {
        "deviceId",
        "feedbackLong",
        "validSleep",
        "inputContext",
        "primaryActivityTracker",
    }
)

# Six parallel `<factor>FactorPercent` / `<factor>FactorFeedback` pairs
# Garmin emits on a readiness reading. Each pair collapses into a single
# `{score, qualifier}` envelope inside the `factors` block.
_TRAINING_READINESS_FACTOR_NAMES = (
    "sleepScore",
    "recoveryTime",
    "acwr",
    "stressHistory",
    "hrv",
    "sleepHistory",
)

# Public-name → (Percent-key, Feedback-key) for the factor envelope. The
# public name drops the `Score` suffix on `sleepScore` so the envelope
# label is the bare factor (`sleep`, `recovery`, …). The `recovery` key
# also lines up with the surviving raw `recoveryTime_min` field.
_TRAINING_READINESS_FACTORS = {
    "sleep": ("sleepScoreFactorPercent", "sleepScoreFactorFeedback"),
    "recovery": ("recoveryTimeFactorPercent", "recoveryTimeFactorFeedback"),
    "acwr": ("acwrFactorPercent", "acwrFactorFeedback"),
    "stress": ("stressHistoryFactorPercent", "stressHistoryFactorFeedback"),
    "hrv": ("hrvFactorPercent", "hrvFactorFeedback"),
    "sleepHistory": ("sleepHistoryFactorPercent", "sleepHistoryFactorFeedback"),
}


def _trim_readiness_reading(reading: Any) -> Any:
    """Trim a single readiness reading dict.

    - Renames `recoveryTime` (minutes) to `recoveryTime_min` for unit clarity.
    - Collapses the six parallel `<factor>FactorPercent`/`<factor>FactorFeedback`
      pairs into a `factors` block of `{score, qualifier}` envelopes.
    - Drops `recoveryTimeChangePhrase` only when null (the common case);
      a non-null phrase is a meaningful narrative signal.
    - Drops `_TRAINING_READINESS_DROP` keys + every consumed factor field.

    Non-dict inputs and error stubs pass through unchanged.
    """
    if not isinstance(reading, dict):
        return reading
    if "error" in reading:
        return reading

    drop = set(_TRAINING_READINESS_DROP)
    for percent_key, feedback_key in _TRAINING_READINESS_FACTORS.values():
        drop.add(percent_key)
        drop.add(feedback_key)
    if reading.get("recoveryTimeChangePhrase") is None:
        drop.add("recoveryTimeChangePhrase")

    out: dict[str, Any] = {k: v for k, v in reading.items() if k not in drop}

    if "recoveryTime" in out:
        out["recoveryTime_min"] = out.pop("recoveryTime")

    factors: dict[str, Any] = {}
    for label, (percent_key, feedback_key) in _TRAINING_READINESS_FACTORS.items():
        if percent_key in reading or feedback_key in reading:
            factors[label] = {
                "score": reading.get(percent_key),
                "qualifier": reading.get(feedback_key),
            }
    if factors:
        out["factors"] = factors

    return out


def trim_training_readiness(payload: Any, *, most_recent_only: bool = True) -> Any:
    """Trim a `get_training_readiness` response.

    The upstream returns a list of one-or-more readings per day (Garmin
    logs a check at wake-up plus updates on real-time variable changes).
    By default the trim returns just the most-recent reading as a single
    dict — the LLM almost always wants "what's the latest" rather than
    "show me every check today". Pass `most_recent_only=False` to keep
    the full list (each entry trimmed); the verbose tool path bypasses
    this helper entirely and returns the un-modified upstream.

    Per-reading trim:
      - Renames `recoveryTime` (minutes) to `recoveryTime_min` for unit
        clarity.
      - Collapses six parallel `<factor>FactorPercent`/`<factor>FactorFeedback`
        pairs into a `factors` block of `{score, qualifier}` envelopes,
        keyed by `sleep` / `recovery` / `acwr` / `stress` / `hrv` /
        `sleepHistory`.
      - Drops `deviceId`, `feedbackLong` (the verbose code; `feedbackShort`
        is kept), `validSleep` (always true when a score exists),
        `inputContext`, `primaryActivityTracker`, `timestampLocal` (lowercase
        `Local` parallel that escapes the case-sensitive `_strip_pii`
        suffix predicate).
      - Drops `recoveryTimeChangePhrase` when null; a non-null phrase
        survives.
      - PII (`userProfilePK`) drops via `_strip_pii` at the final pass.

    Non-list / non-dict inputs and error stubs pass through unchanged.
    """
    if isinstance(payload, dict):
        return _strip_pii(_trim_readiness_reading(payload))
    if not isinstance(payload, list):
        return payload

    if most_recent_only:
        if not payload:
            return {}
        # Garmin emits readings newest-first on the live endpoint, so the
        # first element is the most-recent reading. We trust that ordering
        # rather than re-sorting on `timestamp` — keeps behaviour
        # deterministic against an empty / malformed timestamp. Callers
        # who want to verify ordering or inspect older readings should
        # pass `verbose=True` to get the un-modified upstream list.
        return _strip_pii(_trim_readiness_reading(payload[0]))

    return [_strip_pii(_trim_readiness_reading(r)) for r in payload]


# Per-discipline noise dropped from `mostRecentVO2Max`. `fitnessAge` /
# `fitnessAgeDescription` are perpetually null on Kamil's data and add
# byte tax with no analytical signal.
_VO2_MAX_DISCIPLINE_DROP = frozenset(
    {
        "fitnessAge",
        "fitnessAgeDescription",
    }
)


def _trim_vo2_discipline(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    return {k: v for k, v in value.items() if k not in _VO2_MAX_DISCIPLINE_DROP}


def trim_vo2_max(payload: Any) -> Any:
    """Extract & trim the VO2 max sub-block from a training-status payload.

    The legacy `get_max_metrics` Garmin endpoint returns `[]` for users
    on newer Connect versions — the data has migrated under
    `mostRecentVO2Max` on `get_training_status`. This helper takes the
    full training-status payload and emits a tidy
    `{calendarDate, generic, cycling}` shape:

      - `generic.vo2MaxValue` (integer for narration) and
        `vo2MaxPreciseValue` (decimal for trend analysis) both survive.
      - `fitnessAge` / `fitnessAgeDescription` drop on every discipline
        (perpetually null on the data we've seen).
      - `heatAltitudeAcclimation` is dropped — it's a separate concern
        and surfaces via `get_training_status` for users who need it.
      - `userId` drops via `_strip_pii`.

    Non-dict inputs and error stubs pass through unchanged. When
    `mostRecentVO2Max` is null or the inner `generic` block is null, an
    empty `{}` is returned cleanly.
    """
    if not isinstance(payload, dict):
        return payload
    if "error" in payload:
        return payload

    block = payload.get("mostRecentVO2Max")
    if not isinstance(block, dict):
        return {}

    generic = _trim_vo2_discipline(block.get("generic"))
    cycling = _trim_vo2_discipline(block.get("cycling"))

    if not isinstance(generic, dict) and not isinstance(cycling, dict):
        return {}

    out: dict[str, Any] = {}
    if isinstance(generic, dict):
        # `calendarDate` lives inside `generic` on the live payload — lift
        # it to the top so the consumer sees it without traversing.
        if "calendarDate" in generic:
            out["calendarDate"] = generic["calendarDate"]
        out["generic"] = {k: v for k, v in generic.items() if k != "calendarDate"}
    if isinstance(cycling, dict):
        out["cycling"] = {k: v for k, v in cycling.items() if k != "calendarDate"}
    elif "cycling" in block:
        out["cycling"] = block["cycling"]  # null passes through explicitly

    return _strip_pii(out)


# Always-drop noise on each `calendarItems[]` entry returned by
# `list_scheduled_workouts`. Garmin's `/calendar/year/{y}/month/{m}` endpoint
# returns a union over every possible calendar-item type — scheduled workouts,
# completed activities, naps, badges, dives, courses, hikes, swims — and each
# entry carries every field from every variant, with everything not applicable
# set to null. On a real month with a handful of items the response is ~75K
# chars; ~70 of the ~100 per-item fields are always null for workouts.
#
# Three buckets:
#
# 1. Fields verified null across all 33 items in Kamil's live sample (mixed
#    workouts, activities, naps). Safe to drop unconditionally — they only
#    populate for item types we don't expose via this tool.
# 2. Dive / badge / social / wellness / swim / pack fields that *can* be
#    non-null for the right item type but are never workout-relevant. Dropping
#    these even on activity items keeps the payload focused on scheduling
#    rather than completed-activity reporting (the dedicated activity tools
#    are the right surface for that).
# 3. UI hints and redundant alternate keys (`hasSplits`, `autoCalcCalories`,
#    `workoutUuid` — `workoutId` is the canonical reference).
#
# Per-entry null fields are dropped after this denylist by the inline
# `v is not None` filter in `_trim_scheduled_workout_item`, so fields that
# *could* be informative (e.g. `recurrenceId` for a recurring schedule)
# survive when set but don't pay the null-noise tax when not.
#
# `*Local` timestamp suffixes (e.g. `napStartTimeLocal`, `eventTimeLocal`,
# `startTimestampLocal`) are intentionally NOT listed here — they're handled
# by `_strip_pii`'s `*Local` predicate as a single source of truth across
# every trimmer.
_SCHEDULED_WORKOUT_DROP_ALWAYS = frozenset(
    {
        # Dive
        "diveNumber",
        "maxDepth",
        "avgDepth",
        "surfaceInterval",
        "bottomTime",
        "decoDive",
        # Badge
        "userBadgeId",
        "badgeCategoryTypeId",
        "badgeCategoryTypeDesc",
        "badgeAwardedDate",
        "badgeViewed",
        "hideBadge",
        # Social / sharing
        "shareableEventUuid",
        "shareableEvent",
        "subscribed",
        "primaryEvent",
        # Wellness
        "wellnessActivityUuid",
        # Swim
        "unitOfPoolLength",
        "strokes",
        # Hike / pack
        "beginPackWeight",
        "maxPackWeight",
        # Other always-null in the live sample (verified across 33 items)
        "atpPlanId",
        "avgRespirationRate",
        "completionTarget",
        "courseId",
        "courseName",
        "difference",
        "differenceStress",
        "floorsClimbed",
        "groupId",
        "isRace",
        "isStart",
        "location",
        "maxGradeValue",
        "parentId",
        "splitSummaryMode",
        "url",
        "weight",
        # UI hints / redundant
        "hasSplits",
        "autoCalcCalories",
        "workoutUuid",
    }
)


def _trim_scheduled_workout_item(item: Any) -> Any:
    if not isinstance(item, dict):
        return item
    if "error" in item:
        return item
    trimmed = {
        k: v
        for k, v in item.items()
        if k not in _SCHEDULED_WORKOUT_DROP_ALWAYS and v is not None
    }
    return trimmed


def trim_scheduled_workouts(payload: Any) -> Any:
    """Trim the calendar payload returned by `list_scheduled_workouts`.

    The upstream `GET /calendar/year/{y}/month/{m}` response is a dict with
    a `calendarItems` list — each entry a union over every possible calendar
    item type (workout, activity, nap, badge, dive, course, …) with all
    inapplicable fields set to null. On a typical month this is ~75K chars
    for a handful of real items; the trim drops ~50 always-null/irrelevant
    fields per entry (dive depth, badge metadata, swim strokes, pack weight,
    social-share UUIDs, etc.) plus any field whose value is null on a given
    entry, yielding a >5x reduction in practice.

    Retained per entry: `id` (scheduled-workout id), `workoutId`, `title`,
    `date`, `sportTypeKey`, `itemType`, `trainingPlanId`, plus any of
    `protectedWorkoutSchedule`, `phasedTrainingPlan`, `activityTypeId`,
    `duration`, `distance`, `calories`, `elapsedDuration`, `averageHR`,
    `maxSpeed`, `isParent`, `lapCount`, `noOfSplits`, `totalAscent`,
    `climbDuration`, `activeSets`, `recurrenceId` (when populated).

    Non-dict inputs (e.g. error stubs from `safe_call`) pass through
    unchanged.
    """
    if not isinstance(payload, dict):
        return payload
    if "error" in payload:
        return payload

    items = payload.get("calendarItems")
    if not isinstance(items, list):
        return _strip_pii(payload)

    trimmed_items = [_trim_scheduled_workout_item(item) for item in items]
    out = {**payload, "calendarItems": trimmed_items}
    return _strip_pii(out)


# `get_workout` / `list_workouts` return the workout-template DTO(s). Three
# kinds of noise dominate these payloads, all safe to drop everywhere they
# appear — top level *and* nested inside every step:
#   - `author`: an empty `{}` on user-authored workouts (and, on list items,
#     a thin profile stub); never carries anything the LLM reasons over.
#   - `strokeType` / `equipmentType`: per-step swim / gear enums that are the
#     all-zero default (`{...TypeId: 0}`) on every running step.
#   - `displayOrder` / `displayable`: UI sort / render hints baked into each
#     `sportType` / `stepType` / `endCondition` / `targetType` enum block —
#     repeated 4+ times per step, never analytically meaningful.
# Owner identity (`ownerId`, …) is dropped by the shared `_strip_pii` pass.
_WORKOUT_DROP_ALWAYS = frozenset(
    {
        "author",
        "strokeType",
        "equipmentType",
        "displayOrder",
        "displayable",
    }
)


def _trim_workout_node(obj: Any) -> Any:
    """Recursively drop `_WORKOUT_DROP_ALWAYS` keys at every nesting level.

    Workout DTOs nest the same noise enums inside `workoutSegments[] ->
    workoutSteps[] -> (nested repeat groups) -> ...`, so the drop has to
    recurse rather than touch only the top level. The load-bearing step
    fields — targets (`targetValueOne/Two`, `zoneNumber`), end conditions,
    and the repeat structure (`numberOfIterations`, `workoutSteps`) — are
    preserved.
    """
    if isinstance(obj, dict):
        if "error" in obj:
            return obj
        return {
            k: _trim_workout_node(v)
            for k, v in obj.items()
            if k not in _WORKOUT_DROP_ALWAYS
        }
    if isinstance(obj, list):
        return [_trim_workout_node(item) for item in obj]
    return obj


def trim_workout(payload: Any) -> Any:
    """Trim a single workout-template DTO from `get_workout`.

    Drops the per-step `strokeType` / `equipmentType` defaults, the
    `displayOrder` / `displayable` UI hints on every enum block, and the
    empty `author` object, then strips owner identity via `_strip_pii`.
    Retained: `workoutId`, `workoutName`, `sportType`, the created / updated
    dates, `estimatedDurationInSecs`, and the full `workoutSegments` step
    tree (step types, end conditions, targets, repeat groups).

    Non-dict inputs (e.g. `safe_call` error stubs) pass through unchanged.
    """
    if not isinstance(payload, dict):
        return payload
    if "error" in payload:
        return payload
    return _strip_pii(_trim_workout_node(payload))


def trim_workouts(payload: Any) -> Any:
    """Trim the workout-library list from `list_workouts`.

    The upstream `GET /workout-service/workouts` response is a list of
    workout-template DTOs (same shape as `get_workout`); each item is
    trimmed with the same rules as `trim_workout`. Non-list inputs (e.g.
    error stubs) pass through unchanged.
    """
    if not isinstance(payload, list):
        return payload
    return [trim_workout(item) for item in payload]
