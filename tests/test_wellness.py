"""Tests for the wellness tools — focused on get_sleep / get_stress / get_body_battery payload trimming."""

from __future__ import annotations

import json

from garmin_mcp.tools._trimmers import (
    _compute_stress_buckets,
    trim_body_battery,
    trim_daily_summary,
    trim_hrv,
    trim_sleep,
    trim_steps,
    trim_stress,
)
from tests.conftest import get_tool, load_fixture

# ---------------------------------------------------------------------------
# trim_sleep helper — top-level drop / keep
# ---------------------------------------------------------------------------


def test_trim_sleep_drops_per_minute_streams() -> None:
    full = load_fixture("sleep_payload_full")
    trimmed = trim_sleep(full)

    for dropped in (
        "sleepMovement",
        "sleepRestlessMoments",
        "wellnessEpochRespirationDataDTOList",
        "wellnessEpochRespirationAveragesList",
        "sleepHeartRate",
        "sleepStress",
        "sleepBodyBattery",
        "hrvData",
        "breathingDisruptionData",
    ):
        assert dropped not in trimmed, f"{dropped} should be dropped from top level"


def test_trim_sleep_keeps_summary_top_level() -> None:
    full = load_fixture("sleep_payload_full")
    trimmed = trim_sleep(full)

    # Top-level summary fields preserved.
    assert "dailySleepDTO" in trimmed
    assert "sleepLevels" in trimmed
    assert len(trimmed["sleepLevels"]) == 4
    assert trimmed["restlessMomentsCount"] == 12
    assert trimmed["avgOvernightHrv"] == 55
    assert trimmed["hrvStatus"] == "BALANCED"
    assert trimmed["bodyBatteryChange"] == 38
    assert trimmed["avgSkinTempDeviationC"] == 0.2
    assert trimmed["restingHeartRate"] == 48
    assert trimmed["remSleepData"] is True


# ---------------------------------------------------------------------------
# trim_sleep helper — dailySleepDTO filtering
# ---------------------------------------------------------------------------


def test_trim_sleep_keeps_daily_sleep_dto_required_fields() -> None:
    full = load_fixture("sleep_payload_full")
    trimmed = trim_sleep(full)
    daily = trimmed["dailySleepDTO"]

    # Required fields the LLM needs to reason about a night.
    for kept in (
        "calendarDate",
        "sleepStartTimestampLocal",
        "sleepEndTimestampLocal",
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
    ):
        assert kept in daily, f"{kept} should be kept inside dailySleepDTO"

    # Sleep need / score values pass through intact.
    assert daily["sleepTimeSeconds"] == 26400
    assert daily["sleepScores"]["overall"]["value"] == 78
    assert daily["sleepScores"]["overall"]["qualifierKey"] == "GOOD"
    assert daily["sleepNeed"]["actual"] == 28800
    assert daily["nextSleepNeed"]["feedback"] == "RECOVER_AFTER_HARD_WORKOUT"
    # SpO2 is the primary signal for sleep-apnea screening / nocturnal
    # hypoxia — kept by an explicit decision (M1 from review).
    assert daily["averageSpo2Value"] == 96
    assert daily["lowestSpo2Value"] == 90


def test_trim_sleep_drops_daily_sleep_dto_noise() -> None:
    full = load_fixture("sleep_payload_full")
    trimmed = trim_sleep(full)
    daily = trimmed["dailySleepDTO"]

    # Algorithm internals & redundant aliases gone. The dropped-fields list
    # is the canonical place that documents "this is intentional, not an
    # oversight" — every field that's in the fixture but neither kept by
    # the trim nor in the keep-fields test should appear here.
    for dropped in (
        "id",
        "userProfilePK",
        "sleepWindowConfirmed",
        "sleepWindowConfirmationType",
        "sleepFromDevice",
        "sleepResultTypePK",
        "sleepQualityTypePK",
        "deviceRemCapable",
        "retro",
        "ageGroup",
        "sleepVersion",
        "respirationVersion",
        "skinTempCalibrationDays",
        "averageStressDuringSleep",
        "sleepValidation",
        "autoSleepStartTimestampGMT",
        "autoSleepEndTimestampGMT",
        "startTimestampGMT",
        "startTimestampLocal",
    ):
        assert dropped not in daily, f"{dropped} should be dropped from dailySleepDTO"


def test_trim_sleep_keeps_all_sleep_score_stages() -> None:
    """Every per-stage score (with its qualifierKey) survives — only the
    algorithm normal-range fields get stripped per stage. The qualifierKey
    is an LLM-readable judgment and worth ~200 bytes for the whole block.
    """
    full = load_fixture("sleep_payload_full")
    trimmed = trim_sleep(full)
    scores = trimmed["dailySleepDTO"]["sleepScores"]

    # All stages preserved.
    assert set(scores.keys()) == {
        "totalDuration",
        "stress",
        "awakeCount",
        "overall",
        "remPercentage",
        "lightPercentage",
        "deepPercentage",
    }

    # Qualifier keys + values intact.
    assert scores["totalDuration"]["qualifierKey"] == "GOOD"
    assert scores["awakeCount"]["qualifierKey"] == "EXCELLENT"
    assert scores["deepPercentage"]["qualifierKey"] == "FAIR"
    assert scores["remPercentage"]["value"] == 75

    # Algorithm normal-range fields stripped from every stage that had them.
    for stage in ("remPercentage", "lightPercentage", "deepPercentage"):
        for dropped in ("idealStartInSeconds", "idealEndInSeconds", "optimalStart", "optimalEnd"):
            assert dropped not in scores[stage], f"{stage}.{dropped} should be stripped"


def test_trim_sleep_strips_normal_ranges_from_each_stage() -> None:
    """Constructed payload: every stage carrying normal-range fields gets stripped."""
    payload = {
        "dailySleepDTO": {
            "sleepScores": {
                "overall": {"value": 78, "qualifierKey": "GOOD"},
                "remPercentage": {
                    "value": 75,
                    "qualifierKey": "GOOD",
                    "optimalStart": 21.0,
                    "optimalEnd": 31.0,
                    "idealStartInSeconds": 5544,
                    "idealEndInSeconds": 8184,
                },
            }
        }
    }
    scores = trim_sleep(payload)["dailySleepDTO"]["sleepScores"]
    assert scores["overall"] == {"value": 78, "qualifierKey": "GOOD"}
    assert scores["remPercentage"] == {"value": 75, "qualifierKey": "GOOD"}


def test_trim_sleep_passes_through_non_dict() -> None:
    assert trim_sleep(None) is None
    assert trim_sleep("oops") == "oops"


def test_trim_sleep_passes_through_error_stub() -> None:
    """Error stubs pass through so the diagnostic isn't swallowed by the allowlist."""
    err = {"error": "fetch_failed", "message": "boom", "section": "sleep"}
    assert trim_sleep(err) == err


def test_trim_sleep_handles_missing_daily_sleep_dto() -> None:
    """Top-level `dailySleepDTO` missing → returned dict simply has no DTO."""
    payload = {"sleepLevels": [], "restingHeartRate": 50}
    trimmed = trim_sleep(payload)
    assert trimmed == {"sleepLevels": [], "restingHeartRate": 50}


# ---------------------------------------------------------------------------
# get_sleep tool — verbose flag, payload size, and cache behavior
# ---------------------------------------------------------------------------


def test_get_sleep_default_payload_is_trimmed(mcp_with_tools, mock_garmin) -> None:
    """Default call drops streams and is dramatically smaller than upstream."""
    full = load_fixture("sleep_payload_full")
    mock_garmin.get_sleep_data.return_value = full

    fn = get_tool(mcp_with_tools, "get_sleep")
    result = fn(date="2026-05-09")

    upstream_size = len(json.dumps(full))
    trimmed_size = len(json.dumps(result))
    # The fixture is small (~7KB) — measured ~2.8x reduction with the
    # current trim (keeps per-stage sleepScores qualifiers). Floor of
    # 2.5x catches silent regressions: a trim that drops only one stream
    # field would yield ~1.2x and trip this. On real ~74KB responses
    # where the streams dominate the reduction is dramatically larger.
    assert trimmed_size * 2.5 < upstream_size, (
        f"trimmed={trimmed_size} chars vs upstream={upstream_size}; "
        "expected at least 2.5x reduction on the fixture"
    )

    for dropped in (
        "sleepMovement",
        "sleepRestlessMoments",
        "wellnessEpochRespirationDataDTOList",
        "wellnessEpochRespirationAveragesList",
        "sleepHeartRate",
        "sleepStress",
        "sleepBodyBattery",
        "hrvData",
        "breathingDisruptionData",
    ):
        assert dropped not in result

    # Required signals present.
    assert result["dailySleepDTO"]["sleepTimeSeconds"] == 26400
    assert result["dailySleepDTO"]["sleepScores"]["overall"]["value"] == 78
    assert result["sleepLevels"]
    assert result["restingHeartRate"] == 48


def test_get_sleep_verbose_returns_unmodified_upstream(mcp_with_tools, mock_garmin) -> None:
    """verbose=True returns a value-equal copy of the full upstream payload."""
    full = load_fixture("sleep_payload_full")
    mock_garmin.get_sleep_data.return_value = full

    fn = get_tool(mcp_with_tools, "get_sleep")
    result = fn(date="2026-05-09", verbose=True)

    assert result == full


def test_get_sleep_cache_persists_full_upstream(mcp_with_tools, mock_garmin, monkeypatch) -> None:
    """A verbose=True call after a verbose=False call hits the cache.

    The trim runs *outside* the cache layer, so cached entries always store
    the full upstream and a later verbose=True call does not re-fetch from
    Garmin.
    """
    from garmin_mcp import cache

    monkeypatch.delenv("GARMIN_MCP_NO_CACHE", raising=False)
    cache.clear_all()

    full = load_fixture("sleep_payload_full")
    mock_garmin.get_sleep_data.return_value = full

    fn = get_tool(mcp_with_tools, "get_sleep")

    trimmed = fn(date="2026-05-09")
    verbose = fn(date="2026-05-09", verbose=True)

    # Garmin hit exactly once across both calls.
    assert mock_garmin.get_sleep_data.call_count == 1
    # And the second call returned the full upstream — proof the cache
    # stored the un-trimmed payload, not the trimmed one.
    assert verbose == full
    # Sanity: the trimmed output is still trimmed.
    assert "sleepMovement" not in trimmed
    assert "sleepMovement" in verbose

    cache.clear_all()


def test_get_sleep_default_no_verbose_kwarg_works(mcp_with_tools, mock_garmin) -> None:
    """Calling without `verbose` defaults to the trimmed response."""
    fn = get_tool(mcp_with_tools, "get_sleep")
    result = fn(date="2026-05-09")
    # Mock returns the small `sleep` fixture by default; trim it.
    assert "dailySleepDTO" in result
    assert result["dailySleepDTO"]["calendarDate"] == "2026-05-09"


def test_get_sleep_passes_through_garmin_error(mcp_with_tools, mock_garmin) -> None:
    """A Garmin SDK exception bubbles into a structured error via safe_call."""
    from garminconnect import GarminConnectAuthenticationError

    mock_garmin.get_sleep_data.side_effect = GarminConnectAuthenticationError("expired")

    fn = get_tool(mcp_with_tools, "get_sleep")
    result = fn(date="2026-05-09")
    assert result["error"] == "auth_expired"


# ---------------------------------------------------------------------------
# trim_stress helper — top-level drop / keep
# ---------------------------------------------------------------------------


def test_trim_stress_drops_streams_and_body_battery_and_chart_hints() -> None:
    """The denylist is the canonical place that documents `this is intentional`."""
    full = load_fixture("stress_payload_full")
    trimmed = trim_stress(full)

    for dropped in (
        # Streams + bundled body-battery payload (body battery has its own tool).
        "stressValuesArray",
        "bodyBatteryValuesArray",
        "bodyBatteryValueDescriptorsDTOList",
        "bodyBatteryActivityEvent",
        "bodyBatteryDynamicFeedbackEvent",
        "endOfDayBodyBatteryDynamicFeedbackEvent",
        # Chart-layout hints (Garmin Connect UI rendering).
        "stressChartValueOffset",
        "stressChartYAxisOrigin",
        # Descriptor metadata — the trimmed output uses named keys directly.
        "stressValueDescriptorsDTOList",
        # PII / auth-leaking.
        "userProfilePK",
    ):
        assert dropped not in trimmed, f"{dropped} should be dropped from trimmed stress"


def test_trim_stress_keeps_summary_top_level() -> None:
    full = load_fixture("stress_payload_full")
    trimmed = trim_stress(full)

    assert trimmed["calendarDate"] == "2026-05-09"
    assert trimmed["startTimestampGMT"] == 1746745200000
    assert trimmed["startTimestampLocal"] == 1746748800000
    assert trimmed["endTimestampGMT"] == 1746745200000 + 24 * 3_600_000
    assert trimmed["endTimestampLocal"] == 1746748800000 + 24 * 3_600_000
    assert trimmed["avgStressLevel"] == 28
    assert trimmed["maxStressLevel"] == 90
    # Duration-breakdown summary fields pass through (denylist preserves them).
    assert trimmed["stressDuration"] == 21600
    assert trimmed["restStressDuration"] == 14400
    assert trimmed["lowStressDuration"] == 18000
    assert trimmed["mediumStressDuration"] == 7200
    assert trimmed["highStressDuration"] == 3600


# Top-level keys we currently emit from `trim_stress`. The trimmer uses a
# denylist (so future Garmin summary fields auto-pass-through), but that
# also means a future *bloat* field (another per-minute stream, another
# `bodyBattery*` sibling) would leak through silently. The regression
# test below asserts the trimmed output is a subset of this set so adding
# a new bloat field to the fixture forces an explicit decision: either
# add it to `EXPECTED_TOP_LEVEL_KEYS` (intentional pass-through) or to a
# denylist (intentional drop).
EXPECTED_STRESS_TOP_LEVEL_KEYS = frozenset(
    {
        "calendarDate",
        "startTimestampGMT",
        "startTimestampLocal",
        "endTimestampGMT",
        "endTimestampLocal",
        "avgStressLevel",
        "maxStressLevel",
        "stressDuration",
        "restStressDuration",
        "lowStressDuration",
        "mediumStressDuration",
        "highStressDuration",
        "activityStressDuration",
        "uncategorizedStressDuration",
        "stressBuckets",
    }
)


def test_trim_stress_top_level_keys_subset_of_expected() -> None:
    """Lock in the denylist tradeoff with a regression canary.

    If Garmin (or a future fixture update) introduces a top-level field
    we haven't classified, `trim_stress` will pass it through and this
    test will fail — forcing an explicit add-to-denylist or
    add-to-allowlist decision instead of silent leakage.
    """
    full = load_fixture("stress_payload_full")
    trimmed = trim_stress(full)
    leaked = set(trimmed.keys()) - EXPECTED_STRESS_TOP_LEVEL_KEYS
    assert not leaked, (
        f"trim_stress leaked unclassified fields: {sorted(leaked)}. "
        "Either add them to EXPECTED_STRESS_TOP_LEVEL_KEYS (pass-through) "
        "or to _STRESS_DROP_DEFAULT / _STRESS_DROP_ALWAYS (drop)."
    )


def test_trim_stress_unknown_fixture_field_is_caught_by_canary() -> None:
    """Sanity: the canary above actually flags an unknown field.

    Adds a synthetic `someNewBloatField` to a copy of the fixture and
    confirms the subset assertion would fail. Guards against the canary
    being silently broken in a future refactor.
    """
    full = dict(load_fixture("stress_payload_full"))
    full["someNewBloatField"] = [1, 2, 3]
    trimmed = trim_stress(full)
    leaked = set(trimmed.keys()) - EXPECTED_STRESS_TOP_LEVEL_KEYS
    assert leaked == {"someNewBloatField"}


# ---------------------------------------------------------------------------
# trim_stress helper — hourly bucket aggregation
# ---------------------------------------------------------------------------


def test_trim_stress_emits_24_hourly_buckets() -> None:
    """Fixture has samples in every hour 0..23 → 24 buckets."""
    full = load_fixture("stress_payload_full")
    trimmed = trim_stress(full)

    buckets = trimmed["stressBuckets"]
    assert len(buckets) == 24
    assert [b["hour"] for b in buckets] == list(range(24))


def test_trim_stress_bucket_aggregation_correctness() -> None:
    """Spot-check the edge-case buckets that the fixture was built around."""
    full = load_fixture("stress_payload_full")
    buckets = {b["hour"]: b for b in trim_stress(full)["stressBuckets"]}

    # Hour 0: [50, -1] → one measured sample, one unmeasured.
    assert buckets[0] == {
        "hour": 0,
        "avgStress": 50,
        "maxStress": 50,
        "sampleCount": 2,
        "unmeasuredCount": 1,
    }
    # Hour 1: [60, 70] → avg differs from max.
    assert buckets[1] == {
        "hour": 1,
        "avgStress": 65,
        "maxStress": 70,
        "sampleCount": 2,
        "unmeasuredCount": 0,
    }
    # Hour 3: all-unmeasured (-1, -1) → avg/max null, unmeasuredCount=2.
    assert buckets[3] == {
        "hour": 3,
        "avgStress": None,
        "maxStress": None,
        "sampleCount": 2,
        "unmeasuredCount": 2,
    }
    # Hour 4: -2 (no contact) is unmeasured too.
    assert buckets[4] == {
        "hour": 4,
        "avgStress": 25,
        "maxStress": 25,
        "sampleCount": 2,
        "unmeasuredCount": 1,
    }
    # Hour 5: high-stress bucket.
    assert buckets[5] == {
        "hour": 5,
        "avgStress": 85,
        "maxStress": 90,
        "sampleCount": 2,
        "unmeasuredCount": 0,
    }
    # Hour 23: default fixture pattern [20, 24].
    assert buckets[23] == {
        "hour": 23,
        "avgStress": 22,
        "maxStress": 24,
        "sampleCount": 2,
        "unmeasuredCount": 0,
    }


def test_trim_stress_skips_buckets_with_zero_samples() -> None:
    """A day with samples in only some hours emits only the populated buckets."""
    payload = {
        "startTimestampGMT": 0,
        "stressValuesArray": [
            [0, 30],                  # hour 0
            [3 * 3_600_000, 40],      # hour 3
            [3 * 3_600_000 + 60_000, 50],  # hour 3 again
        ],
    }
    buckets = trim_stress(payload)["stressBuckets"]
    assert [b["hour"] for b in buckets] == [0, 3]
    assert buckets[1]["avgStress"] == 45
    assert buckets[1]["sampleCount"] == 2


def test_trim_stress_drops_samples_outside_day_window() -> None:
    """Samples outside [start, start+24h) are skipped when no end_ts given.

    Without `endTimestampGMT` the bucket cap falls back to 24h.
    """
    payload = {
        "startTimestampGMT": 0,
        "stressValuesArray": [
            [0, 30],                          # hour 0 — kept
            [25 * 3_600_000, 99],             # hour 25 — dropped
            [-1 * 3_600_000, 99],             # negative offset — dropped
        ],
    }
    buckets = trim_stress(payload)["stressBuckets"]
    assert len(buckets) == 1
    assert buckets[0]["hour"] == 0


def test_trim_stress_dst_fall_back_25h_day_keeps_extra_hour() -> None:
    """On DST fall-back days (25h local day, e.g. last Sunday of October
    in Poland) the bucket cap is derived from `endTimestampGMT - startTimestampGMT`,
    so an `hour=24` bucket is emitted instead of being silently dropped.
    """
    HOUR_MS = 3_600_000
    payload = {
        "calendarDate": "2026-10-25",
        "startTimestampGMT": 0,
        "endTimestampGMT": 25 * HOUR_MS,
        "stressValuesArray": [
            [0, 30],                  # hour 0
            [12 * HOUR_MS, 40],       # hour 12
            [24 * HOUR_MS, 50],       # hour 24 — DST extra hour, kept
            [25 * HOUR_MS, 99],       # hour 25 — past day end, still dropped
        ],
    }
    buckets = trim_stress(payload)["stressBuckets"]
    assert [b["hour"] for b in buckets] == [0, 12, 24]
    assert buckets[-1] == {
        "hour": 24,
        "avgStress": 50,
        "maxStress": 50,
        "sampleCount": 1,
        "unmeasuredCount": 0,
    }


def test_trim_stress_dst_spring_forward_23h_day_drops_skipped_hour() -> None:
    """On DST spring-forward days (23h local day) `endTimestampGMT - start`
    is 23h, so an `hour=23` sample is correctly dropped (the local clock
    skipped that hour).
    """
    HOUR_MS = 3_600_000
    payload = {
        "calendarDate": "2026-03-29",
        "startTimestampGMT": 0,
        "endTimestampGMT": 23 * HOUR_MS,
        "stressValuesArray": [
            [22 * HOUR_MS, 40],       # hour 22 — kept
            [23 * HOUR_MS, 99],       # hour 23 — past day end, dropped
        ],
    }
    buckets = trim_stress(payload)["stressBuckets"]
    assert [b["hour"] for b in buckets] == [22]


def test_trim_stress_excludes_bool_levels() -> None:
    """Python `bool` is a subclass of `int`, so a True/False level would
    aggregate as 1/0 stress without an explicit guard. Bools are skipped
    entirely (counted in neither measured nor unmeasured).
    """
    payload = {
        "startTimestampGMT": 0,
        "endTimestampGMT": 3_600_000,
        "stressValuesArray": [
            [0, 50],
            [60_000, True],          # skipped — would otherwise count as level=1
            [120_000, False],        # skipped — would otherwise count as level=0
        ],
    }
    buckets = trim_stress(payload)["stressBuckets"]
    assert buckets == [
        {
            "hour": 0,
            "avgStress": 50,
            "maxStress": 50,
            "sampleCount": 1,
            "unmeasuredCount": 0,
        }
    ]


def test_trim_stress_treats_nan_level_as_unmeasured() -> None:
    """A NaN level would otherwise propagate through `sum` and crash
    `round()`. NaN is treated as unmeasured, just like -1 / -2.
    """
    payload = {
        "startTimestampGMT": 0,
        "endTimestampGMT": 3_600_000,
        "stressValuesArray": [
            [0, 50],
            [60_000, float("nan")],  # NaN → unmeasured
        ],
    }
    buckets = trim_stress(payload)["stressBuckets"]
    assert buckets == [
        {
            "hour": 0,
            "avgStress": 50,
            "maxStress": 50,
            "sampleCount": 2,
            "unmeasuredCount": 1,
        }
    ]


def test_trim_stress_accepts_float_start_ts() -> None:
    """Garmin's parallel `startTimestampLocal` field can come back as a
    float in some encodings — the bucket helper should accept either.
    """
    payload = {
        "startTimestampGMT": 0.0,
        "endTimestampGMT": 3_600_000.0,
        "stressValuesArray": [[0, 50]],
    }
    buckets = trim_stress(payload)["stressBuckets"]
    assert len(buckets) == 1
    assert buckets[0]["avgStress"] == 50


def test_trim_stress_omits_buckets_when_no_samples() -> None:
    """An empty / missing stressValuesArray produces no `stressBuckets` key."""
    payload = {"calendarDate": "2026-05-09", "avgStressLevel": 22}
    trimmed = trim_stress(payload)
    assert "stressBuckets" not in trimmed
    assert trimmed["calendarDate"] == "2026-05-09"


def test_trim_stress_omits_buckets_when_start_ts_missing() -> None:
    """Without a start timestamp we can't bucket — skip the field rather than emit junk."""
    payload = {
        "calendarDate": "2026-05-09",
        "stressValuesArray": [[1746745200000, 30]],
    }
    trimmed = trim_stress(payload)
    assert "stressBuckets" not in trimmed


# ---------------------------------------------------------------------------
# trim_stress helper — ISO 8601 timestamp coercion (production format)
# ---------------------------------------------------------------------------


def test_trim_stress_handles_iso_timestamp_format() -> None:
    """The live Garmin API returns ISO strings for `start/endTimestampGMT`
    while the samples in `stressValuesArray` stay Unix-ms. PR #9 shipped a
    fixture that was Unix-ms throughout, so the type guard `isinstance(..,
    (int, float))` rejected the live ISO strings and `stressBuckets` was
    silently dropped in production. This regression test loads a fixture
    that matches production reality and asserts buckets ARE emitted.
    """
    full = load_fixture("stress_payload_iso_timestamps")
    trimmed = trim_stress(full)

    # The bug: this assert would fail before the fix because buckets was None.
    assert "stressBuckets" in trimmed
    buckets = trimmed["stressBuckets"]
    assert len(buckets) == 24
    assert [b["hour"] for b in buckets] == list(range(24))

    # Spot-check a few buckets to confirm the same aggregation as the
    # Unix-ms fixture (same sample data, same expected output).
    bucket_by_hour = {b["hour"]: b for b in buckets}
    assert bucket_by_hour[0] == {
        "hour": 0,
        "avgStress": 50,
        "maxStress": 50,
        "sampleCount": 2,
        "unmeasuredCount": 1,
    }
    assert bucket_by_hour[5] == {
        "hour": 5,
        "avgStress": 85,
        "maxStress": 90,
        "sampleCount": 2,
        "unmeasuredCount": 0,
    }


def test_compute_stress_buckets_iso_string_start_ts() -> None:
    """Direct unit test against the exact production string format
    (`'2026-05-08T22:00:00.0'` — implicit GMT, single-tenth fractional).
    """
    HOUR_MS = 3_600_000
    # 2026-05-08T22:00:00 UTC == 1778277600000 Unix-ms
    base_ms = 1778277600000
    samples = [
        [base_ms, 30],
        [base_ms + HOUR_MS, 50],
        [base_ms + 2 * HOUR_MS, 70],
    ]
    buckets = _compute_stress_buckets(
        samples,
        "2026-05-08T22:00:00.0",
        "2026-05-09T22:00:00.0",
    )
    assert buckets is not None
    assert [b["hour"] for b in buckets] == [0, 1, 2]
    assert buckets[0]["avgStress"] == 30
    assert buckets[1]["avgStress"] == 50
    assert buckets[2]["avgStress"] == 70


def test_compute_stress_buckets_iso_string_without_fractional() -> None:
    """ISO strings without a trailing fractional also parse (defensive —
    Garmin's format has a `.0` today but the surrounding spec doesn't
    require it).
    """
    base_ms = 1778277600000
    buckets = _compute_stress_buckets(
        [[base_ms, 42]],
        "2026-05-08T22:00:00",
        "2026-05-09T22:00:00",
    )
    assert buckets is not None
    assert buckets[0]["hour"] == 0
    assert buckets[0]["avgStress"] == 42


def test_compute_stress_buckets_unparseable_string_returns_none() -> None:
    """Garbage strings still yield None rather than crashing."""
    samples = [[0, 30]]
    assert _compute_stress_buckets(samples, "not-a-date", None) is None
    assert _compute_stress_buckets(samples, "", None) is None


def test_compute_stress_buckets_unix_ms_path_unchanged() -> None:
    """Round-trip guard: the existing Unix-ms call site behaves identically
    after the helper is applied (no regression for the PR #9 path).
    """
    HOUR_MS = 3_600_000
    samples = [[0, 30], [HOUR_MS, 60]]
    buckets = _compute_stress_buckets(samples, 0, 2 * HOUR_MS)
    assert buckets == [
        {
            "hour": 0,
            "avgStress": 30,
            "maxStress": 30,
            "sampleCount": 1,
            "unmeasuredCount": 0,
        },
        {
            "hour": 1,
            "avgStress": 60,
            "maxStress": 60,
            "sampleCount": 1,
            "unmeasuredCount": 0,
        },
    ]


def test_trim_stress_passes_through_non_dict() -> None:
    assert trim_stress(None) is None
    assert trim_stress("oops") == "oops"


def test_trim_stress_passes_through_error_stub() -> None:
    """Error stubs pass through so the diagnostic isn't swallowed by the trim."""
    err = {"error": "fetch_failed", "message": "boom", "section": "stress"}
    assert trim_stress(err) == err


# ---------------------------------------------------------------------------
# get_stress tool — verbose flag, payload size, cache behavior
# ---------------------------------------------------------------------------


def test_get_stress_default_payload_is_trimmed(mcp_with_tools, mock_garmin) -> None:
    """Default call drops streams + body-battery + chart hints + descriptors."""
    full = load_fixture("stress_payload_full")
    mock_garmin.get_stress_data.return_value = full

    fn = get_tool(mcp_with_tools, "get_stress")
    result = fn(date="2026-05-09")

    for dropped in (
        "stressValuesArray",
        "bodyBatteryValuesArray",
        "bodyBatteryValueDescriptorsDTOList",
        "bodyBatteryActivityEvent",
        "bodyBatteryDynamicFeedbackEvent",
        "endOfDayBodyBatteryDynamicFeedbackEvent",
        "stressChartValueOffset",
        "stressChartYAxisOrigin",
        "stressValueDescriptorsDTOList",
        "userProfilePK",
    ):
        assert dropped not in result

    # Required signals present + the new aggregation.
    assert result["calendarDate"] == "2026-05-09"
    assert result["avgStressLevel"] == 28
    assert result["maxStressLevel"] == 90
    assert len(result["stressBuckets"]) == 24


def test_get_stress_realistic_density_shrinks_payload(mcp_with_tools, mock_garmin) -> None:
    """On a real-sized 480-sample payload the trim is a >5x reduction.

    The hand-checkable fixture has only 48 samples — the 24-bucket
    aggregation we *add* is ~as verbose as 48 raw samples we *remove*,
    so the fixture-based size comparison isn't meaningful. This test
    builds a realistic 480-sample payload (3-min interval x 24h, what
    Garmin actually returns) and verifies the dramatic shrinkage that
    motivates this PR.
    """
    HOUR_MS = 3_600_000
    THREE_MIN_MS = 3 * 60 * 1000
    start = 1746745200000
    samples = [
        [start + i * THREE_MIN_MS, (i % 50) + 10]  # cycle through 10..59
        for i in range(480)
    ]
    bb_verbose = [
        [start + i * THREE_MIN_MS, (i % 100), "RESTING", 1.0] for i in range(480)
    ]
    full = {
        "userProfilePK": 12345,
        "calendarDate": "2026-05-09",
        "startTimestampGMT": start,
        "startTimestampLocal": start + HOUR_MS,
        "endTimestampGMT": start + 24 * HOUR_MS,
        "endTimestampLocal": start + 25 * HOUR_MS,
        "avgStressLevel": 28,
        "maxStressLevel": 90,
        "stressValuesArray": samples,
        "bodyBatteryValuesArray": bb_verbose,
        "stressValueDescriptorsDTOList": [
            {"key": "timestamp", "index": 0},
            {"key": "stressLevel", "index": 1},
        ],
    }
    mock_garmin.get_stress_data.return_value = full

    fn = get_tool(mcp_with_tools, "get_stress")
    result = fn(date="2026-05-09")

    upstream_size = len(json.dumps(full))
    trimmed_size = len(json.dumps(result))
    assert trimmed_size * 5 < upstream_size, (
        f"trimmed={trimmed_size} chars vs upstream={upstream_size}; "
        "expected at least 5x reduction on a realistic 480-sample payload"
    )


def test_get_stress_verbose_returns_unmodified_upstream(mcp_with_tools, mock_garmin) -> None:
    """verbose=True returns a value-equal copy of the full upstream payload."""
    full = load_fixture("stress_payload_full")
    mock_garmin.get_stress_data.return_value = full

    fn = get_tool(mcp_with_tools, "get_stress")
    result = fn(date="2026-05-09", verbose=True)

    assert result == full
    assert "stressValuesArray" in result
    assert "bodyBatteryValuesArray" in result
    assert "stressBuckets" not in result


def test_get_stress_cache_persists_full_upstream(mcp_with_tools, mock_garmin, monkeypatch) -> None:
    """A verbose=True call after a verbose=False call hits the cache.

    The trim runs *outside* the cache layer, so cached entries always store
    the full upstream and a later verbose=True call does not re-fetch.
    """
    from garmin_mcp import cache

    monkeypatch.delenv("GARMIN_MCP_NO_CACHE", raising=False)
    cache.clear_all()

    full = load_fixture("stress_payload_full")
    mock_garmin.get_stress_data.return_value = full

    fn = get_tool(mcp_with_tools, "get_stress")

    trimmed = fn(date="2026-05-09")
    verbose = fn(date="2026-05-09", verbose=True)

    assert mock_garmin.get_stress_data.call_count == 1
    assert verbose == full
    assert "stressValuesArray" not in trimmed
    assert "stressValuesArray" in verbose

    cache.clear_all()


def test_get_stress_default_no_verbose_kwarg_works(mcp_with_tools, mock_garmin) -> None:
    """Calling without `verbose` defaults to the trimmed response."""
    fn = get_tool(mcp_with_tools, "get_stress")
    result = fn(date="2026-05-09")
    # Mock returns the small `stress` fixture by default; trim it.
    assert result["calendarDate"] == "2026-05-09"


def test_get_stress_passes_through_garmin_error(mcp_with_tools, mock_garmin) -> None:
    """A Garmin SDK exception bubbles into a structured error via safe_call."""
    from garminconnect import GarminConnectAuthenticationError

    mock_garmin.get_stress_data.side_effect = GarminConnectAuthenticationError("expired")

    fn = get_tool(mcp_with_tools, "get_stress")
    result = fn(date="2026-05-09")
    assert result["error"] == "auth_expired"


# ---------------------------------------------------------------------------
# trim_body_battery helper — top-level drop / keep
# ---------------------------------------------------------------------------


def test_trim_body_battery_drops_descriptor_and_internal_metadata() -> None:
    """The allowlist drops descriptor lists, profile PII, and internal version."""
    full = load_fixture("body_battery_payload_full")
    trimmed = trim_body_battery(full)

    entry = trimmed[0]
    for dropped in (
        # Descriptor metadata — the trimmed output uses named keys directly
        # for everything else and the values array's element shape is
        # well-known (timestamp + level).
        "bodyBatteryValueDescriptorDTOList",
        "bodyBatteryValueDescriptorsDTOList",
        # PII / auth-leaking.
        "userProfilePK",
        "userProfilePk",
        # Internal algorithm version (always 3 in the wild).
        "bodyBatteryVersion",
    ):
        assert dropped not in entry, f"{dropped} should be dropped from body-battery entry"


def test_trim_body_battery_keeps_summary_top_level() -> None:
    full = load_fixture("body_battery_payload_full")
    trimmed = trim_body_battery(full)
    entry = trimmed[0]

    assert entry["date"] == "2026-05-09"
    assert entry["charged"] == 65
    assert entry["drained"] == 28
    assert entry["startTimestampGMT"] == 1746745200000
    assert entry["startTimestampLocal"] == 1746748800000
    assert entry["endTimestampGMT"] == 1746831600000
    assert entry["endTimestampLocal"] == 1746835200000


def test_trim_body_battery_keeps_compressed_transition_array() -> None:
    """The 2-tuple `[ts, level]` transition list survives intact —
    it's the high-signal summary the LLM uses to read drain/recharge points.
    """
    full = load_fixture("body_battery_payload_full")
    trimmed = trim_body_battery(full)
    entry = trimmed[0]

    assert "bodyBatteryValuesArray" in entry
    array = entry["bodyBatteryValuesArray"]
    assert len(array) == 6
    assert array[0] == [1746745200000, 30]
    assert array[-1] == [1746829800000, 67]
    # Every entry is a 2-tuple — the compressed transition shape, not the
    # 4-tuple per-3-min verbose form (which is bundled in the stress endpoint).
    assert all(len(pair) == 2 for pair in array)


# Top-level keys the trim emits for a per-day body-battery entry. The trim
# uses an allowlist (see `_BB_TOP_LEVEL_KEEP`), so a future Garmin field
# we haven't classified would be silently dropped. This canary asserts the
# trimmed output's keys are a *subset* of this set — i.e. the trim doesn't
# emit anything we haven't blessed.
EXPECTED_BB_TOP_LEVEL_KEYS = frozenset(
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


def test_trim_body_battery_top_level_keys_subset_of_expected() -> None:
    """Allowlist regression canary.

    If a refactor accidentally widens `_BB_TOP_LEVEL_KEEP` to pass through
    a noisy field, this catches it. Mirrors the stress trim's canary.
    """
    full = load_fixture("body_battery_payload_full")
    trimmed = trim_body_battery(full)
    entry = trimmed[0]
    leaked = set(entry.keys()) - EXPECTED_BB_TOP_LEVEL_KEYS
    assert not leaked, (
        f"trim_body_battery emitted unclassified fields: {sorted(leaked)}. "
        "Either add them to EXPECTED_BB_TOP_LEVEL_KEYS or remove them from "
        "_BB_TOP_LEVEL_KEEP in _trimmers.py."
    )


# ---------------------------------------------------------------------------
# trim_body_battery helper — activity event sub-trim
# ---------------------------------------------------------------------------


def test_trim_body_battery_keeps_activity_events_with_required_fields() -> None:
    """Activity events tie body-battery deltas to specific activities — kept.

    Each entry retains the analytic fields (event type, time, duration,
    impact, feedback, optional activity reference) and drops device/audit
    metadata.
    """
    full = load_fixture("body_battery_payload_full")
    trimmed = trim_body_battery(full)
    events = trimmed[0]["bodyBatteryActivityEvent"]

    assert len(events) == 3

    sleep_event = events[0]
    assert sleep_event["eventType"] == "SLEEP"
    assert sleep_event["eventStartTimeGmt"] == 1746745200000
    assert sleep_event["durationInMilliseconds"] == 21600000
    assert sleep_event["bodyBatteryImpact"] == 38
    assert sleep_event["feedbackType"] == "GOOD_SLEEP"
    assert sleep_event["shortFeedback"] == "GOOD_SLEEP"

    # Activity-tied event preserves the linked activity reference.
    activity_event = events[1]
    assert activity_event["eventType"] == "ACTIVITY"
    assert activity_event["bodyBatteryImpact"] == -15
    assert activity_event["activityName"] == "Morning Run"
    assert activity_event["activityType"] == "running"
    assert activity_event["activityId"] == 1112223334


def test_trim_body_battery_drops_activity_event_device_metadata() -> None:
    """Device IDs, audit timestamps, timezone offsets are dropped per event."""
    full = load_fixture("body_battery_payload_full")
    trimmed = trim_body_battery(full)
    events = trimmed[0]["bodyBatteryActivityEvent"]

    for event in events:
        for dropped in ("deviceId", "eventUpdateTimeGmt", "timezoneOffset"):
            assert dropped not in event, f"{dropped} should be dropped from activity event"


# ---------------------------------------------------------------------------
# trim_body_battery helper — dynamic feedback event sub-trim
# ---------------------------------------------------------------------------


def test_trim_body_battery_dynamic_feedback_keeps_both_short_and_long_type() -> None:
    """Both `feedbackShortType` and `feedbackLongType` are kept.

    The longType is sometimes a verbose suffix of the shortType (e.g.
    TYPICAL → WITHIN_TYPICAL_RANGE_FOR_THIS_TIME_OF_DAY) but in other
    events it's a multi-tag composite carrying narrative context not
    derivable from the shortType — see the end-of-day event below where
    shortType is "TYPICAL" (a single enum) but longType is
    "SLEEP_PREPARATION_STRESSFUL_AND_EXERCISE_AND_BB_LOW" (the densest
    "why was BB low" signal). Bytes per event are negligible.
    """
    full = load_fixture("body_battery_payload_full")
    trimmed = trim_body_battery(full)
    entry = trimmed[0]

    feedback = entry["bodyBatteryDynamicFeedbackEvent"]
    assert feedback == {
        "eventTimestampGmt": 1746788400000,
        "bodyBatteryLevel": "WITHIN_TYPICAL_RANGE",
        "feedbackShortType": "TYPICAL",
        "feedbackLongType": "WITHIN_TYPICAL_RANGE_FOR_THIS_TIME_OF_DAY",
    }

    end_of_day = entry["endOfDayBodyBatteryDynamicFeedbackEvent"]
    assert end_of_day == {
        "eventTimestampGmt": 1746828000000,
        "bodyBatteryLevel": "WITHIN_TYPICAL_RANGE",
        "feedbackShortType": "TYPICAL",
        "feedbackLongType": "SLEEP_PREPARATION_STRESSFUL_AND_EXERCISE_AND_BB_LOW",
    }


# ---------------------------------------------------------------------------
# trim_body_battery helper — input shape handling
# ---------------------------------------------------------------------------


def test_trim_body_battery_maps_over_multi_day_list() -> None:
    """A multi-day response (one entry per day) is trimmed entry-by-entry."""
    full = load_fixture("body_battery_payload_full")
    day1 = full[0]
    day2 = dict(day1)
    day2["date"] = "2026-05-10"
    day2["charged"] = 80
    day2["drained"] = 20
    payload = [day1, day2]

    trimmed = trim_body_battery(payload)
    assert len(trimmed) == 2
    assert trimmed[0]["date"] == "2026-05-09"
    assert trimmed[1]["date"] == "2026-05-10"
    assert trimmed[1]["charged"] == 80
    # Both entries had their descriptor metadata dropped.
    assert "bodyBatteryValueDescriptorDTOList" not in trimmed[0]
    assert "bodyBatteryValueDescriptorDTOList" not in trimmed[1]


def test_trim_body_battery_handles_single_day_dict() -> None:
    """Defensive: also accept a bare dict, not just a list of dicts."""
    full = load_fixture("body_battery_payload_full")
    entry = full[0]
    trimmed = trim_body_battery(entry)
    assert isinstance(trimmed, dict)
    assert trimmed["date"] == "2026-05-09"
    assert "userProfilePK" not in trimmed


def test_trim_body_battery_passes_through_error_stub() -> None:
    """Error stubs in a list pass through so the diagnostic isn't swallowed."""
    err = {"error": "fetch_failed", "message": "boom", "section": "body_battery"}
    assert trim_body_battery([err]) == [err]
    assert trim_body_battery(err) == err


def test_trim_body_battery_passes_through_non_dict_non_list() -> None:
    assert trim_body_battery(None) is None
    assert trim_body_battery("oops") == "oops"


def test_trim_body_battery_handles_missing_optional_fields() -> None:
    """Entries with no activity events / no feedback events still work."""
    payload = [
        {
            "date": "2026-05-09",
            "charged": 50,
            "drained": 30,
            "userProfilePK": 999,
            "bodyBatteryValuesArray": [[0, 50]],
        }
    ]
    trimmed = trim_body_battery(payload)
    entry = trimmed[0]
    assert entry["date"] == "2026-05-09"
    assert entry["charged"] == 50
    assert "userProfilePK" not in entry
    assert "bodyBatteryActivityEvent" not in entry
    assert "bodyBatteryDynamicFeedbackEvent" not in entry


# ---------------------------------------------------------------------------
# get_body_battery tool — verbose flag, payload size, cache behavior
# ---------------------------------------------------------------------------


def test_get_body_battery_default_payload_is_trimmed(mcp_with_tools, mock_garmin) -> None:
    """Default call drops descriptor metadata, internal version, profile PII."""
    full = load_fixture("body_battery_payload_full")
    mock_garmin.get_body_battery.return_value = full

    fn = get_tool(mcp_with_tools, "get_body_battery")
    result = fn(start_date="2026-05-09")

    entry = result[0]
    for dropped in (
        "userProfilePK",
        "bodyBatteryValueDescriptorDTOList",
        "bodyBatteryVersion",
    ):
        assert dropped not in entry

    # Required signals present.
    assert entry["date"] == "2026-05-09"
    assert entry["charged"] == 65
    assert entry["drained"] == 28
    assert len(entry["bodyBatteryValuesArray"]) == 6
    assert len(entry["bodyBatteryActivityEvent"]) == 3
    assert entry["bodyBatteryDynamicFeedbackEvent"]["feedbackShortType"] == "TYPICAL"
    # Both short and long feedback codes survive — the long form sometimes
    # carries multi-tag narrative context not derivable from the short.
    assert (
        entry["bodyBatteryDynamicFeedbackEvent"]["feedbackLongType"]
        == "WITHIN_TYPICAL_RANGE_FOR_THIS_TIME_OF_DAY"
    )


def test_get_body_battery_default_shrinks_fixture_payload(mcp_with_tools, mock_garmin) -> None:
    """The fixture-level reduction is modest (~1.5x) — it's a small payload
    with one day of dropped metadata. The bigger savings show up in the
    realistic-density test below; this test just establishes a non-trivial
    floor so a regression that disables trimming is caught.
    """
    full = load_fixture("body_battery_payload_full")
    mock_garmin.get_body_battery.return_value = full

    fn = get_tool(mcp_with_tools, "get_body_battery")
    result = fn(start_date="2026-05-09")

    upstream_size = len(json.dumps(full))
    trimmed_size = len(json.dumps(result))
    assert trimmed_size < upstream_size, (
        f"trimmed={trimmed_size} chars vs upstream={upstream_size}; "
        "expected at least some reduction on the fixture"
    )


def test_get_body_battery_realistic_density_shrinks_payload(
    mcp_with_tools, mock_garmin
) -> None:
    """A realistic 7-day range shrinks meaningfully (~1.3x).

    The body-battery endpoint, unlike stress, does NOT bundle a 480-entry
    per-3-min sample stream — Garmin returns only the compressed 6-12
    transition list. So the trim's savings come from per-day repeated
    metadata (descriptor list, userProfilePK, bodyBatteryVersion,
    per-event device/audit fields) rather than a stream we can collapse.
    Floor of 1.3x catches a regression that disables trimming entirely
    (ratio would be ~1.0) without falsely flagging the modest reduction
    the actual upstream allows.
    """
    HOUR_MS = 3_600_000
    full = []
    for day in range(7):
        start = 1746745200000 + day * 86_400_000
        full.append(
            {
                "userProfilePK": 12345,
                "date": f"2026-05-{9 + day:02d}",
                "charged": 60,
                "drained": 30,
                "startTimestampGMT": start,
                "startTimestampLocal": start + HOUR_MS,
                "endTimestampGMT": start + 24 * HOUR_MS,
                "endTimestampLocal": start + 25 * HOUR_MS,
                "bodyBatteryVersion": 3,
                "bodyBatteryValueDescriptorDTOList": [
                    {"bodyBatteryValueDescriptorIndex": 0,
                     "bodyBatteryValueDescriptorKey": "timestamp"},
                    {"bodyBatteryValueDescriptorIndex": 1,
                     "bodyBatteryValueDescriptorKey": "bodyBatteryLevel"},
                ],
                "bodyBatteryValuesArray": [
                    [start + i * 4 * HOUR_MS, 30 + i * 5] for i in range(6)
                ],
                "bodyBatteryActivityEvent": [
                    {
                        "eventType": "SLEEP",
                        "eventStartTimeGmt": start,
                        "eventUpdateTimeGmt": start + 6 * HOUR_MS,
                        "timezoneOffset": HOUR_MS,
                        "durationInMilliseconds": 6 * HOUR_MS,
                        "bodyBatteryImpact": 38,
                        "feedbackType": "GOOD_SLEEP",
                        "shortFeedback": "GOOD_SLEEP",
                        "deviceId": 9876543210,
                        "activityName": None,
                        "activityType": None,
                        "activityId": None,
                    }
                ] * 3,
                "bodyBatteryDynamicFeedbackEvent": {
                    "eventTimestampGmt": start + 12 * HOUR_MS,
                    "bodyBatteryLevel": "WITHIN_TYPICAL_RANGE",
                    "feedbackShortType": "TYPICAL",
                    "feedbackLongType": "WITHIN_TYPICAL_RANGE_FOR_THIS_TIME_OF_DAY",
                },
                "endOfDayBodyBatteryDynamicFeedbackEvent": {
                    "eventTimestampGmt": start + 23 * HOUR_MS,
                    "bodyBatteryLevel": "WITHIN_TYPICAL_RANGE",
                    "feedbackShortType": "TYPICAL",
                    "feedbackLongType": "SLEEP_PREPARATION_STRESSFUL_AND_EXERCISE_AND_BB_LOW",
                },
            }
        )
    mock_garmin.get_body_battery.return_value = full

    fn = get_tool(mcp_with_tools, "get_body_battery")
    result = fn(start_date="2026-05-09", end_date="2026-05-15")

    upstream_size = len(json.dumps(full))
    trimmed_size = len(json.dumps(result))
    assert trimmed_size * 1.3 < upstream_size, (
        f"trimmed={trimmed_size} chars vs upstream={upstream_size}; "
        "expected at least 1.3x reduction on a 7-day realistic payload"
    )


def test_get_body_battery_verbose_returns_unmodified_upstream(
    mcp_with_tools, mock_garmin
) -> None:
    """verbose=True returns a value-equal copy of the full upstream payload."""
    full = load_fixture("body_battery_payload_full")
    mock_garmin.get_body_battery.return_value = full

    fn = get_tool(mcp_with_tools, "get_body_battery")
    result = fn(start_date="2026-05-09", verbose=True)

    assert result == full
    # Fields dropped by the default trim are present in the verbose copy.
    assert "userProfilePK" in result[0]
    assert "bodyBatteryValueDescriptorDTOList" in result[0]
    assert "bodyBatteryVersion" in result[0]


def test_get_body_battery_cache_persists_full_upstream(
    mcp_with_tools, mock_garmin, monkeypatch
) -> None:
    """verbose=True after verbose=False hits the cache, returns full upstream.

    The trim runs *outside* the cache layer, so cached entries always store
    the full upstream and a later verbose=True call does not re-fetch from
    Garmin.
    """
    from garmin_mcp import cache

    monkeypatch.delenv("GARMIN_MCP_NO_CACHE", raising=False)
    cache.clear_all()

    full = load_fixture("body_battery_payload_full")
    mock_garmin.get_body_battery.return_value = full

    fn = get_tool(mcp_with_tools, "get_body_battery")

    trimmed = fn(start_date="2026-05-09")
    verbose = fn(start_date="2026-05-09", verbose=True)

    assert mock_garmin.get_body_battery.call_count == 1
    assert verbose == full
    assert "userProfilePK" not in trimmed[0]
    assert "userProfilePK" in verbose[0]

    cache.clear_all()


def test_get_body_battery_default_no_verbose_kwarg_works(mcp_with_tools, mock_garmin) -> None:
    """Calling without `verbose` defaults to the trimmed response."""
    fn = get_tool(mcp_with_tools, "get_body_battery")
    result = fn(start_date="2026-05-09")
    # Mock returns the small `body_battery` fixture by default — already
    # has no descriptor metadata, but the trim should still succeed.
    assert isinstance(result, list)
    assert result[0]["date"] == "2026-05-09"


def test_get_body_battery_passes_through_garmin_error(mcp_with_tools, mock_garmin) -> None:
    """A Garmin SDK exception bubbles into a structured error via safe_call."""
    from garminconnect import GarminConnectAuthenticationError

    mock_garmin.get_body_battery.side_effect = GarminConnectAuthenticationError("expired")

    fn = get_tool(mcp_with_tools, "get_body_battery")
    result = fn(start_date="2026-05-09")
    assert result["error"] == "auth_expired"


def test_get_body_battery_date_range_propagates_to_garmin(
    mcp_with_tools, mock_garmin
) -> None:
    """When end_date is provided, both dates flow to the Garmin SDK call."""
    full = load_fixture("body_battery_payload_full")
    mock_garmin.get_body_battery.return_value = full

    fn = get_tool(mcp_with_tools, "get_body_battery")
    fn(start_date="2026-05-01", end_date="2026-05-09")

    mock_garmin.get_body_battery.assert_called_once_with("2026-05-01", "2026-05-09")


# ---------------------------------------------------------------------------
# trim_steps helper — drop / collapse behavior
# ---------------------------------------------------------------------------


# Top-level keys we expect to remain in every surviving steps entry. The
# trim drops `pushes` and `activityLevelConstant` and passes everything
# else through, so a future Garmin field would leak through silently. The
# canary below asserts every entry's keys are a subset of this set — adding
# a new bloat field to the fixture forces an explicit decision (extend the
# expected set or extend `_STEPS_DROP_PER_ENTRY`).
EXPECTED_STEPS_ENTRY_KEYS = frozenset(
    {"startGMT", "endGMT", "steps", "primaryActivityLevel"}
)


def test_trim_steps_drops_pushes_and_activity_level_constant() -> None:
    """The drop-list is the canonical place that documents `this is intentional`."""
    full = load_fixture("steps_payload_full")
    trimmed = trim_steps(full)

    assert trimmed, "fixture should produce some buckets"
    for entry in trimmed:
        # intentional, not oversight
        assert "pushes" not in entry, "pushes should be dropped from every bucket"
        assert (
            "activityLevelConstant" not in entry
        ), "activityLevelConstant should be dropped from every bucket"


def test_trim_steps_entry_keys_subset_of_expected() -> None:
    """Pass-through regression canary.

    `trim_steps` keeps every field other than the explicit drop list, so a
    new Garmin field would silently leak through. This catches it — adding
    a key to the fixture without classifying it (either extend
    EXPECTED_STEPS_ENTRY_KEYS or extend `_STEPS_DROP_PER_ENTRY`) will
    fail this test.
    """
    full = load_fixture("steps_payload_full")
    trimmed = trim_steps(full)
    leaked: set[str] = set()
    for entry in trimmed:
        leaked |= set(entry.keys()) - EXPECTED_STEPS_ENTRY_KEYS
    assert not leaked, (
        f"trim_steps leaked unclassified fields: {sorted(leaked)}. "
        "Either add them to EXPECTED_STEPS_ENTRY_KEYS (pass-through) "
        "or to _STEPS_DROP_PER_ENTRY (drop)."
    )


def test_trim_steps_collapses_contiguous_zero_run_same_level() -> None:
    """8 sleeping zero-step buckets collapse into a single span."""
    payload = [
        {
            "startGMT": f"2026-05-09T00:{i*15:02d}:00.0",
            "endGMT": f"2026-05-09T00:{(i+1)*15:02d}:00.0",
            "steps": 0,
            "pushes": 0,
            "primaryActivityLevel": "sleeping",
            "activityLevelConstant": True,
        }
        for i in range(2)
    ] + [
        {
            "startGMT": "2026-05-09T00:30:00.0",
            "endGMT": "2026-05-09T00:45:00.0",
            "steps": 0,
            "pushes": 0,
            "primaryActivityLevel": "sleeping",
            "activityLevelConstant": True,
        }
    ]
    # 3 sleeping zero-buckets [00:00..00:15, 00:15..00:30, 00:30..00:45]
    # should collapse to one [00:00..00:45].
    trimmed = trim_steps(payload)
    assert trimmed == [
        {
            "startGMT": "2026-05-09T00:00:00.0",
            "endGMT": "2026-05-09T00:45:00.0",
            "steps": 0,
            "primaryActivityLevel": "sleeping",
        }
    ]


def test_trim_steps_non_zero_in_middle_splits_run() -> None:
    """A non-zero bucket in the middle of a same-level zero run splits it."""
    payload = [
        {"startGMT": "T0", "endGMT": "T1", "steps": 0, "primaryActivityLevel": "sedentary"},
        {"startGMT": "T1", "endGMT": "T2", "steps": 0, "primaryActivityLevel": "sedentary"},
        {"startGMT": "T2", "endGMT": "T3", "steps": 50, "primaryActivityLevel": "sedentary"},
        {"startGMT": "T3", "endGMT": "T4", "steps": 0, "primaryActivityLevel": "sedentary"},
        {"startGMT": "T4", "endGMT": "T5", "steps": 0, "primaryActivityLevel": "sedentary"},
    ]
    trimmed = trim_steps(payload)
    assert trimmed == [
        {"startGMT": "T0", "endGMT": "T2", "steps": 0, "primaryActivityLevel": "sedentary"},
        {"startGMT": "T2", "endGMT": "T3", "steps": 50, "primaryActivityLevel": "sedentary"},
        {"startGMT": "T3", "endGMT": "T5", "steps": 0, "primaryActivityLevel": "sedentary"},
    ]


def test_trim_steps_activity_level_change_ends_run_even_with_zero_steps() -> None:
    """sleeping → sedentary boundary is preserved even when both sides are zero.

    Wake-up is the kind of transition the LLM reasons over.
    """
    payload = [
        {"startGMT": "T0", "endGMT": "T1", "steps": 0, "primaryActivityLevel": "sleeping"},
        {"startGMT": "T1", "endGMT": "T2", "steps": 0, "primaryActivityLevel": "sleeping"},
        {"startGMT": "T2", "endGMT": "T3", "steps": 0, "primaryActivityLevel": "sedentary"},
        {"startGMT": "T3", "endGMT": "T4", "steps": 0, "primaryActivityLevel": "sedentary"},
    ]
    trimmed = trim_steps(payload)
    assert trimmed == [
        {"startGMT": "T0", "endGMT": "T2", "steps": 0, "primaryActivityLevel": "sleeping"},
        {"startGMT": "T2", "endGMT": "T4", "steps": 0, "primaryActivityLevel": "sedentary"},
    ]


def test_trim_steps_non_zero_buckets_never_collapse() -> None:
    """Two adjacent same-level buckets with steps > 0 stay distinct."""
    payload = [
        {"startGMT": "T0", "endGMT": "T1", "steps": 100, "primaryActivityLevel": "active"},
        {"startGMT": "T1", "endGMT": "T2", "steps": 200, "primaryActivityLevel": "active"},
        {"startGMT": "T2", "endGMT": "T3", "steps": 50, "primaryActivityLevel": "active"},
    ]
    trimmed = trim_steps(payload)
    # Every non-zero bucket survives — analytical signal preserved.
    assert trimmed == payload  # also confirms pushes / activityLevelConstant were absent


def test_trim_steps_passes_through_non_list() -> None:
    assert trim_steps(None) is None
    assert trim_steps("oops") == "oops"
    assert trim_steps({"error": "fetch_failed"}) == {"error": "fetch_failed"}


def test_trim_steps_error_stub_in_list_passes_through() -> None:
    """An error stub inside a list flushes the open run and passes through."""
    err = {"error": "fetch_failed", "message": "boom", "section": "steps"}
    payload = [
        {"startGMT": "T0", "endGMT": "T1", "steps": 0, "primaryActivityLevel": "sleeping"},
        {"startGMT": "T1", "endGMT": "T2", "steps": 0, "primaryActivityLevel": "sleeping"},
        err,
        {"startGMT": "T2", "endGMT": "T3", "steps": 100, "primaryActivityLevel": "active"},
    ]
    trimmed = trim_steps(payload)
    assert trimmed == [
        {"startGMT": "T0", "endGMT": "T2", "steps": 0, "primaryActivityLevel": "sleeping"},
        err,
        {"startGMT": "T2", "endGMT": "T3", "steps": 100, "primaryActivityLevel": "active"},
    ]


def test_trim_steps_empty_list() -> None:
    assert trim_steps([]) == []


# ---------------------------------------------------------------------------
# get_steps tool — verbose flag, payload size, cache behavior
# ---------------------------------------------------------------------------


def test_get_steps_default_payload_is_trimmed(mcp_with_tools, mock_garmin) -> None:
    """Default call drops pushes/activityLevelConstant and collapses zero runs."""
    full = load_fixture("steps_payload_full")
    mock_garmin.get_steps_data.return_value = full

    fn = get_tool(mcp_with_tools, "get_steps")
    result = fn(date="2026-05-09")

    # Collapsing always shrinks the live fixture (96 buckets, ~70% zeros).
    assert len(result) < len(full)
    upstream_size = len(json.dumps(full))
    trimmed_size = len(json.dumps(result))
    # Measured ~3x on Kamil's 2026-05-09 data. Floor of 2x catches a
    # regression that disables collapsing entirely (~1.1x from drop-only).
    assert trimmed_size * 2 < upstream_size, (
        f"trimmed={trimmed_size} chars vs upstream={upstream_size}; "
        "expected at least 2x reduction on the live fixture"
    )

    for entry in result:
        assert "pushes" not in entry
        assert "activityLevelConstant" not in entry


def test_get_steps_verbose_returns_unmodified_upstream(mcp_with_tools, mock_garmin) -> None:
    """verbose=True returns a value-equal copy of the full upstream payload."""
    full = load_fixture("steps_payload_full")
    mock_garmin.get_steps_data.return_value = full

    fn = get_tool(mcp_with_tools, "get_steps")
    result = fn(date="2026-05-09", verbose=True)

    assert result == full
    # Fields the trim drops are still present in the verbose copy.
    assert "pushes" in result[0]
    assert "activityLevelConstant" in result[0]


def test_get_steps_cache_persists_full_upstream(mcp_with_tools, mock_garmin, monkeypatch) -> None:
    """A verbose=True call after a verbose=False call hits the cache.

    The trim runs *outside* the cache layer, so cached entries always store
    the full upstream and a later verbose=True call does not re-fetch from
    Garmin.
    """
    from garmin_mcp import cache

    monkeypatch.delenv("GARMIN_MCP_NO_CACHE", raising=False)
    cache.clear_all()

    full = load_fixture("steps_payload_full")
    mock_garmin.get_steps_data.return_value = full

    fn = get_tool(mcp_with_tools, "get_steps")

    trimmed = fn(date="2026-05-09")
    verbose = fn(date="2026-05-09", verbose=True)

    assert mock_garmin.get_steps_data.call_count == 1
    assert verbose == full
    assert "pushes" not in trimmed[0]
    assert "pushes" in verbose[0]

    cache.clear_all()


def test_get_steps_default_no_verbose_kwarg_works(mcp_with_tools, mock_garmin) -> None:
    """Calling without `verbose` defaults to the trimmed response."""
    fn = get_tool(mcp_with_tools, "get_steps")
    result = fn(date="2026-05-09")
    # Mock returns the small `steps` fixture by default; trim it.
    assert isinstance(result, list)
    for entry in result:
        assert "pushes" not in entry


def test_get_steps_passes_through_garmin_error(mcp_with_tools, mock_garmin) -> None:
    """A Garmin SDK exception bubbles into a structured error via safe_call."""
    from garminconnect import GarminConnectAuthenticationError

    mock_garmin.get_steps_data.side_effect = GarminConnectAuthenticationError("expired")

    fn = get_tool(mcp_with_tools, "get_steps")
    result = fn(date="2026-05-09")
    assert result["error"] == "auth_expired"


# ---------------------------------------------------------------------------
# trim_hrv helper — drop list + passthroughs
# ---------------------------------------------------------------------------


# Top-level keys allowed in the trimmed response. Acts as a canary: a future
# Garmin field appearing at the top level (or one of these keys disappearing)
# fails this test fast so we revisit the trim instead of silently shipping
# the new field.
_EXPECTED_HRV_KEYS = {
    "hrvSummary",
    "startTimestampGMT",
    "startTimestampLocal",
    "endTimestampGMT",
    "endTimestampLocal",
    "sleepStartTimestampGMT",
    "sleepStartTimestampLocal",
    "sleepEndTimestampGMT",
    "sleepEndTimestampLocal",
}


def test_trim_hrv_drops_hrv_readings() -> None:
    full = load_fixture("hrv_payload_full")
    trimmed = trim_hrv(full)
    # Intentional, not oversight — `hrvReadings` is ~90% of the payload.
    assert "hrvReadings" not in trimmed


def test_trim_hrv_drops_user_profile_pk() -> None:
    full = load_fixture("hrv_payload_full")
    trimmed = trim_hrv(full)
    # Intentional, not oversight — parity with trim_stress / trim_body_battery.
    assert "userProfilePk" not in trimmed


def test_trim_hrv_keys_subset_of_expected() -> None:
    """Allowlist canary: every surviving top-level key is in the expected set."""
    full = load_fixture("hrv_payload_full")
    trimmed = trim_hrv(full)
    unexpected = set(trimmed.keys()) - _EXPECTED_HRV_KEYS
    assert not unexpected, (
        f"unexpected top-level keys in trimmed HRV payload: {unexpected}. "
        "Either add to _EXPECTED_HRV_KEYS or extend trim_hrv's drop list."
    )


def test_trim_hrv_keeps_summary_block_untouched() -> None:
    """`hrvSummary` is preserved byte-equivalent to the upstream fixture."""
    full = load_fixture("hrv_payload_full")
    trimmed = trim_hrv(full)
    assert trimmed["hrvSummary"] == full["hrvSummary"]


def test_trim_hrv_keeps_sleep_window_timestamps() -> None:
    full = load_fixture("hrv_payload_full")
    trimmed = trim_hrv(full)
    for kept in (
        "startTimestampGMT",
        "endTimestampGMT",
        "sleepStartTimestampGMT",
        "sleepEndTimestampGMT",
    ):
        assert trimmed[kept] == full[kept]


def test_trim_hrv_passes_through_non_dict_and_empty() -> None:
    """None / non-dict inputs pass through unchanged; empty dict stays empty."""
    assert trim_hrv(None) is None
    assert trim_hrv("oops") == "oops"
    assert trim_hrv([]) == []
    assert trim_hrv({}) == {}


def test_trim_hrv_passes_through_error_stub() -> None:
    """Error stubs from safe_call should NOT be swallowed by the trim.

    The current implementation is a pure denylist (drops `hrvReadings` /
    `userProfilePk`) so a `{"error": ...}` stub passes through structurally
    intact — there's no allowlist to filter the diagnostic out.
    """
    stub = {"error": "fetch_failed", "exception": "RuntimeError: boom"}
    assert trim_hrv(stub) == stub


# ---------------------------------------------------------------------------
# get_hrv tool — verbose flag, cache reuse, error passthrough
# ---------------------------------------------------------------------------


def test_get_hrv_default_payload_is_trimmed(mcp_with_tools, mock_garmin) -> None:
    """Default call drops hrvReadings + userProfilePk and shrinks the payload."""
    full = load_fixture("hrv_payload_full")
    mock_garmin.get_hrv_data.return_value = full

    fn = get_tool(mcp_with_tools, "get_hrv")
    result = fn(date="2026-05-09")

    assert "hrvReadings" not in result
    assert "userProfilePk" not in result
    # Surviving keys stay within the allowlist canary.
    assert set(result.keys()) <= _EXPECTED_HRV_KEYS
    # hrvSummary survives byte-equivalent.
    assert result["hrvSummary"] == full["hrvSummary"]


def test_get_hrv_default_reduction_ratio_floor(mcp_with_tools, mock_garmin) -> None:
    """Trimmed response is at least 5x smaller than the upstream fixture.

    Measured ~10x on Kamil's 2026-05-09 live data (~7200 → ~700 chars).
    Floor of 5x catches a regression that disables the trim or accidentally
    keeps the readings array.
    """
    full = load_fixture("hrv_payload_full")
    mock_garmin.get_hrv_data.return_value = full

    fn = get_tool(mcp_with_tools, "get_hrv")
    trimmed = fn(date="2026-05-09")

    upstream_size = len(json.dumps(full))
    trimmed_size = len(json.dumps(trimmed))
    assert trimmed_size * 5 <= upstream_size, (
        f"trimmed={trimmed_size} chars vs upstream={upstream_size}; "
        "expected at least 5x reduction"
    )


def test_get_hrv_verbose_returns_unmodified_upstream(mcp_with_tools, mock_garmin) -> None:
    """verbose=True returns a value-equal copy of the full upstream payload."""
    full = load_fixture("hrv_payload_full")
    mock_garmin.get_hrv_data.return_value = full

    fn = get_tool(mcp_with_tools, "get_hrv")
    result = fn(date="2026-05-09", verbose=True)

    assert result == full
    assert "hrvReadings" in result
    assert "userProfilePk" in result


def test_get_hrv_cache_persists_full_upstream(mcp_with_tools, mock_garmin, monkeypatch) -> None:
    """A verbose=True call after a verbose=False call hits the cache.

    The trim runs *outside* the cache layer, so cached entries always store
    the full upstream and a later verbose=True call does not re-fetch from
    Garmin.
    """
    from garmin_mcp import cache

    monkeypatch.delenv("GARMIN_MCP_NO_CACHE", raising=False)
    cache.clear_all()

    full = load_fixture("hrv_payload_full")
    mock_garmin.get_hrv_data.return_value = full

    fn = get_tool(mcp_with_tools, "get_hrv")

    trimmed = fn(date="2026-05-09")
    verbose = fn(date="2026-05-09", verbose=True)

    assert mock_garmin.get_hrv_data.call_count == 1
    assert verbose == full
    assert "hrvReadings" not in trimmed
    assert "hrvReadings" in verbose

    cache.clear_all()


def test_get_hrv_passes_through_garmin_error(mcp_with_tools, mock_garmin) -> None:
    """A Garmin SDK exception bubbles into a structured error via safe_call."""
    from garminconnect import GarminConnectAuthenticationError

    mock_garmin.get_hrv_data.side_effect = GarminConnectAuthenticationError("expired")

    fn = get_tool(mcp_with_tools, "get_hrv")
    result = fn(date="2026-05-09")
    assert result["error"] == "auth_expired"


# ---------------------------------------------------------------------------
# trim_daily_summary helper — drop locks + conditional Spo2 / abnormalHr
# ---------------------------------------------------------------------------


# Top-level keys the trim emits on a typical no-Spo2 day. Acts as a canary:
# a future Garmin field appearing in the upstream payload (or one of these
# keys disappearing) fails this test fast so we revisit the trim instead of
# silently shipping the new field. Built from the surviving keys on Kamil's
# 2026-05-09 fixture; an additive Garmin change forces an explicit decision.
_EXPECTED_DAILY_SUMMARY_KEYS = frozenset(
    {
        "calendarDate",
        "dailyStepGoal",
        "durationInMilliseconds",
        # Calorie + distance summary
        "totalKilocalories",
        "activeKilocalories",
        "bmrKilocalories",
        "burnedKilocalories",
        "consumedKilocalories",
        "remainingKilocalories",
        "netCalorieGoal",
        "netRemainingKilocalories",
        "totalDistanceMeters",
        "restingCaloriesFromActivity",
        # Steps + activity-time breakdown
        "totalSteps",
        "highlyActiveSeconds",
        "activeSeconds",
        "sedentarySeconds",
        "sleepingSeconds",
        # Floors
        "floorsAscended",
        "floorsDescended",
        "floorsAscendedInMeters",
        "floorsDescendedInMeters",
        "userFloorsAscendedGoal",
        # Intensity minutes
        "moderateIntensityMinutes",
        "vigorousIntensityMinutes",
        "intensityMinutesGoal",
        # Heart rate
        "minHeartRate",
        "maxHeartRate",
        "restingHeartRate",
        "lastSevenDaysAvgRestingHeartRate",
        "minAvgHeartRate",
        "maxAvgHeartRate",
        # Stress
        "averageStressLevel",
        "maxStressLevel",
        "stressDuration",
        "stressPercentage",
        "stressQualifier",
        "restStressDuration",
        "restStressPercentage",
        "activityStressDuration",
        "activityStressPercentage",
        "uncategorizedStressDuration",
        "uncategorizedStressPercentage",
        "totalStressDuration",
        "lowStressDuration",
        "lowStressPercentage",
        "mediumStressDuration",
        "mediumStressPercentage",
        "highStressDuration",
        "highStressPercentage",
        "measurableAwakeDuration",
        "measurableAsleepDuration",
        # Body battery summary values (the per-day numbers, not the events
        # — those are dropped as cross-tool duplicates)
        "bodyBatteryChargedValue",
        "bodyBatteryDrainedValue",
        "bodyBatteryHighestValue",
        "bodyBatteryLowestValue",
        "bodyBatteryMostRecentValue",
        "bodyBatteryDuringSleep",
        "bodyBatteryAtWakeTime",
        # Spo2 (kept conditionally — see _DAILY_SUMMARY_SPO2_FIELDS)
        "averageSpo2",
        "lowestSpo2",
        "latestSpo2",
        "latestSpo2ReadingTimeGmt",
        "latestSpo2ReadingTimeLocal",
        # Respiration summary (no per-minute stream; just the numbers)
        "avgWakingRespirationValue",
        "highestRespirationValue",
        "lowestRespirationValue",
        "latestRespirationValue",
        "latestRespirationTimeGMT",
        # Wellness window timestamps
        "wellnessStartTimeGmt",
        "wellnessStartTimeLocal",
        "wellnessEndTimeGmt",
        "wellnessEndTimeLocal",
        # Misc
        "averageMonitoringEnvironmentAltitude",
        # Conditional medical signal — kept only when non-null
        "abnormalHeartRateAlertsCount",
    }
)


def test_trim_daily_summary_drops_pii_and_identifiers() -> None:
    """userProfileId / userDailySummaryId / uuid drop unconditionally."""
    full = load_fixture("daily_summary_payload_full")
    result = trim_daily_summary(full)
    # PII / identifiers
    assert "userProfileId" not in result
    assert "userDailySummaryId" not in result
    assert "uuid" not in result


def test_trim_daily_summary_drops_privacy_source_version_metadata() -> None:
    """Privacy / source / algorithm-version / sync metadata drop unconditionally."""
    full = load_fixture("daily_summary_payload_full")
    result = trim_daily_summary(full)
    assert "rule" not in result
    assert "privacyProtected" not in result
    assert "source" not in result
    assert "bodyBatteryVersion" not in result
    assert "respirationAlgorithmVersion" not in result
    assert "wellnessDescription" not in result
    assert "lastSyncTimestampGMT" not in result


def test_trim_daily_summary_drops_includes_flags() -> None:
    """`includes{Wellness,Activity,CalorieConsumed}Data` are UI hints; drop."""
    full = load_fixture("daily_summary_payload_full")
    result = trim_daily_summary(full)
    assert "includesWellnessData" not in result
    assert "includesActivityData" not in result
    assert "includesCalorieConsumedData" not in result


def test_trim_daily_summary_drops_wellness_aliases_keeps_canonical() -> None:
    """`wellness*` aliases drop; the non-alias copy survives byte-equal.

    On every payload we've seen, `wellnessKilocalories == totalKilocalories`,
    `wellnessActiveKilocalories == activeKilocalories`, and
    `wellnessDistanceMeters == totalDistanceMeters`. Drop the alias.
    """
    full = load_fixture("daily_summary_payload_full")
    result = trim_daily_summary(full)
    assert "wellnessKilocalories" not in result
    assert "wellnessActiveKilocalories" not in result
    assert "wellnessDistanceMeters" not in result
    # The non-alias copies survive with their upstream values intact.
    assert result["totalKilocalories"] == 3043.0
    assert result["activeKilocalories"] == 705.0
    assert result["totalDistanceMeters"] == 5829


def test_trim_daily_summary_drops_body_battery_cross_tool_duplicates() -> None:
    """Body-battery sub-payloads are covered by `get_body_battery`; drop here."""
    full = load_fixture("daily_summary_payload_full")
    result = trim_daily_summary(full)
    assert "bodyBatteryActivityEventList" not in result
    assert "bodyBatteryDynamicFeedbackEvent" not in result
    assert "endOfDayBodyBatteryDynamicFeedbackEvent" not in result
    # The per-day body-battery summary *numbers* (not the events) survive —
    # they're cheap and sit alongside the rest of the day's totals.
    assert result["bodyBatteryChargedValue"] == 57
    assert result["bodyBatteryDrainedValue"] == 62
    assert result["bodyBatteryHighestValue"] == 61


def test_trim_daily_summary_drops_spo2_block_when_all_null() -> None:
    """All Spo2 fields drop together when every reading is null."""
    full = load_fixture("daily_summary_payload_full")
    # Sanity-check the fixture matches the precondition (no Spo2 readings).
    assert full["averageSpo2"] is None
    assert full["lowestSpo2"] is None
    assert full["latestSpo2"] is None

    result = trim_daily_summary(full)
    assert "averageSpo2" not in result
    assert "lowestSpo2" not in result
    assert "latestSpo2" not in result
    assert "latestSpo2ReadingTimeGmt" not in result
    assert "latestSpo2ReadingTimeLocal" not in result


def test_trim_daily_summary_keeps_spo2_block_when_any_reading_populated() -> None:
    """A non-null Spo2 reading flips the whole block back on."""
    full = load_fixture("daily_summary_payload_full")
    full = {
        **full,
        "averageSpo2": 96,
        "lowestSpo2": 92,
        "latestSpo2": 95,
        "latestSpo2ReadingTimeGmt": "2026-05-09T05:30:00.0",
        "latestSpo2ReadingTimeLocal": "2026-05-09T07:30:00.0",
    }
    result = trim_daily_summary(full)
    assert result["averageSpo2"] == 96
    assert result["lowestSpo2"] == 92
    assert result["latestSpo2"] == 95
    assert result["latestSpo2ReadingTimeGmt"] == "2026-05-09T05:30:00.0"
    assert result["latestSpo2ReadingTimeLocal"] == "2026-05-09T07:30:00.0"


def test_trim_daily_summary_keeps_spo2_block_when_only_one_reading_populated() -> None:
    """Even a single non-null reading keeps the whole block (no partial drop)."""
    full = load_fixture("daily_summary_payload_full")
    full = {**full, "averageSpo2": 97}  # lowest/latest stay null
    result = trim_daily_summary(full)
    assert result["averageSpo2"] == 97
    # The other Spo2 fields survive (as null) — partial-drop would lose them.
    assert "lowestSpo2" in result
    assert "latestSpo2" in result
    assert "latestSpo2ReadingTimeGmt" in result
    assert "latestSpo2ReadingTimeLocal" in result


def test_trim_daily_summary_drops_abnormal_hr_alerts_when_null() -> None:
    """`abnormalHeartRateAlertsCount` is null on a normal day → drop."""
    full = load_fixture("daily_summary_payload_full")
    assert full["abnormalHeartRateAlertsCount"] is None
    result = trim_daily_summary(full)
    assert "abnormalHeartRateAlertsCount" not in result


def test_trim_daily_summary_keeps_abnormal_hr_alerts_when_populated() -> None:
    """A non-null `abnormalHeartRateAlertsCount` is a medical signal; keep."""
    full = load_fixture("daily_summary_payload_full")
    full = {**full, "abnormalHeartRateAlertsCount": 2}
    result = trim_daily_summary(full)
    assert result["abnormalHeartRateAlertsCount"] == 2


def test_trim_daily_summary_keeps_abnormal_hr_alerts_zero_boundary() -> None:
    """Boundary: `0` (checked, no alerts) is a meaningful signal — keep.

    Pins the `is None` predicate against a future drive-by `if not value:`
    regression that would falsy-collapse `0` into the drop branch and lose
    the "monitoring ran and reported zero alerts" signal.
    """
    full = load_fixture("daily_summary_payload_full")
    full = {**full, "abnormalHeartRateAlertsCount": 0}
    result = trim_daily_summary(full)
    assert result["abnormalHeartRateAlertsCount"] == 0


# Keys that are in `_EXPECTED_DAILY_SUMMARY_KEYS` but may legitimately be
# absent from a trimmed result — the conditional drops fire when the
# upstream readings are null. Excluded from the "required keys present"
# direction of the bidirectional canary so a no-Spo2 / no-abnormal-HR day
# doesn't trip the silent-loss check. The Spo2 conditional + abnormal-HR
# conditional both have their own dedicated tests, so removing them from
# the required-set doesn't weaken coverage.
_DAILY_SUMMARY_CONDITIONAL_KEYS = frozenset(
    {
        "averageSpo2",
        "lowestSpo2",
        "latestSpo2",
        "latestSpo2ReadingTimeGmt",
        "latestSpo2ReadingTimeLocal",
        "abnormalHeartRateAlertsCount",
    }
)


def test_trim_daily_summary_keys_match_expected_bidirectional() -> None:
    """Bidirectional canary: trimmed keys are *exactly* the expected set
    (modulo the conditionally-dropped fields).

    The trim uses a denylist (so additive Garmin fields auto-pass-through),
    but that means a future *bloat* field would leak silently AND a future
    over-aggressive denylist extension would silently drop a should-keep
    field. Both directions:

      - subset check: every emitted key is in `_EXPECTED_DAILY_SUMMARY_KEYS`
        — catches additive bloat (a new upstream key survives the trim).
      - superset check: every required key is present in the result —
        catches silent loss (the trim accidentally drops, say,
        `restingHeartRate`). The conditional Spo2 + abnormal-HR fields are
        excluded from this direction since they're absent on the no-Spo2
        / no-alert fixture by design.
    """
    full = load_fixture("daily_summary_payload_full")
    result = trim_daily_summary(full)

    unexpected = set(result.keys()) - _EXPECTED_DAILY_SUMMARY_KEYS
    assert not unexpected, (
        f"trim emitted unexpected top-level keys: {sorted(unexpected)}; "
        "either add to _EXPECTED_DAILY_SUMMARY_KEYS or add to the denylist"
    )

    required = _EXPECTED_DAILY_SUMMARY_KEYS - _DAILY_SUMMARY_CONDITIONAL_KEYS
    missing = required - set(result.keys())
    assert not missing, (
        f"trim silently dropped required keys: {sorted(missing)}; "
        "the denylist likely over-extended — keep these in the trimmed output"
    )


def test_trim_daily_summary_passes_through_non_dict() -> None:
    """Non-dict / empty / None inputs pass through unchanged."""
    assert trim_daily_summary(None) is None
    assert trim_daily_summary([]) == []
    assert trim_daily_summary({}) == {}
    assert trim_daily_summary("oops") == "oops"


def test_trim_daily_summary_passes_through_error_stub() -> None:
    """Error stubs from `safe_call` aren't swallowed by the trim."""
    stub = {"error": "auth_expired", "remediation": "rotate token"}
    assert trim_daily_summary(stub) == stub


# ---------------------------------------------------------------------------
# get_daily_summary tool — verbose flag, payload size, cache behavior
# ---------------------------------------------------------------------------


def test_get_daily_summary_default_payload_is_trimmed(mcp_with_tools, mock_garmin) -> None:
    """Default call drops PII / wellness aliases / body-battery duplicates."""
    full = load_fixture("daily_summary_payload_full")
    mock_garmin.get_user_summary.return_value = full

    fn = get_tool(mcp_with_tools, "get_daily_summary")
    result = fn(date="2026-05-09")

    # Spot-check one field from each drop category.
    assert "userProfileId" not in result
    assert "wellnessKilocalories" not in result
    assert "bodyBatteryActivityEventList" not in result
    # The kept canary still holds.
    assert set(result.keys()) <= _EXPECTED_DAILY_SUMMARY_KEYS
    # Daily totals survive byte-equivalent.
    assert result["totalSteps"] == full["totalSteps"]
    assert result["totalKilocalories"] == full["totalKilocalories"]


def test_get_daily_summary_default_reduction_ratio_floor(mcp_with_tools, mock_garmin) -> None:
    """Trimmed response is at least 1.5x smaller than the upstream fixture.

    Measured ~1.9x on Kamil's 2026-05-09 live data (~4.1KB → ~2.2KB).
    Floor of 1.5x catches a regression that disables the trim or a payload
    shape that pushes the ratio below the threshold.
    """
    full = load_fixture("daily_summary_payload_full")
    mock_garmin.get_user_summary.return_value = full

    fn = get_tool(mcp_with_tools, "get_daily_summary")
    trimmed = fn(date="2026-05-09")

    upstream_size = len(json.dumps(full))
    trimmed_size = len(json.dumps(trimmed))
    assert trimmed_size * 1.5 <= upstream_size, (
        f"trimmed={trimmed_size} chars vs upstream={upstream_size}; "
        "expected at least 1.5x reduction"
    )


def test_get_daily_summary_verbose_returns_unmodified_upstream(
    mcp_with_tools, mock_garmin
) -> None:
    """verbose=True returns a value-equal copy of the full upstream payload."""
    full = load_fixture("daily_summary_payload_full")
    mock_garmin.get_user_summary.return_value = full

    fn = get_tool(mcp_with_tools, "get_daily_summary")
    result = fn(date="2026-05-09", verbose=True)

    assert result == full
    # Fields the trim drops are still present in the verbose copy.
    assert "userProfileId" in result
    assert "wellnessKilocalories" in result
    assert "bodyBatteryActivityEventList" in result


def test_get_daily_summary_cache_persists_full_upstream(
    mcp_with_tools, mock_garmin, monkeypatch
) -> None:
    """A verbose=True call after a verbose=False call hits the cache.

    The trim runs *outside* the cache layer, so cached entries always store
    the full upstream and a later verbose=True call does not re-fetch from
    Garmin.
    """
    from garmin_mcp import cache

    monkeypatch.delenv("GARMIN_MCP_NO_CACHE", raising=False)
    cache.clear_all()

    full = load_fixture("daily_summary_payload_full")
    mock_garmin.get_user_summary.return_value = full

    fn = get_tool(mcp_with_tools, "get_daily_summary")

    trimmed = fn(date="2026-05-09")
    verbose = fn(date="2026-05-09", verbose=True)

    assert mock_garmin.get_user_summary.call_count == 1
    assert verbose == full
    assert "userProfileId" not in trimmed
    assert "userProfileId" in verbose

    cache.clear_all()


def test_get_daily_summary_default_no_verbose_kwarg_works(
    mcp_with_tools, mock_garmin
) -> None:
    """Calling without `verbose` defaults to the trimmed response."""
    fn = get_tool(mcp_with_tools, "get_daily_summary")
    result = fn(date="2026-05-09")
    # Mock returns the small `daily_summary` fixture by default; it has no
    # bloat fields to drop, so we just check the trimmed response is a dict.
    assert isinstance(result, dict)
    assert "userProfileId" not in result


def test_get_daily_summary_passes_through_garmin_error(mcp_with_tools, mock_garmin) -> None:
    """A Garmin SDK exception bubbles into a structured error via safe_call."""
    from garminconnect import GarminConnectAuthenticationError

    mock_garmin.get_user_summary.side_effect = GarminConnectAuthenticationError("expired")

    fn = get_tool(mcp_with_tools, "get_daily_summary")
    result = fn(date="2026-05-09")
    assert result["error"] == "auth_expired"
