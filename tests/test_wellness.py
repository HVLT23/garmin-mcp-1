"""Tests for the wellness tools — focused on get_sleep payload trimming."""

from __future__ import annotations

import json

from garmin_mcp.tools._trimmers import trim_sleep
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
