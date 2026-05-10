"""Tests for training tools (status, load, readiness, vo2_max, race-predictor)."""

from __future__ import annotations

import copy
import json

import pytest

from garmin_mcp.tools._trimmers import (
    _collapse_device_map,
    trim_training_load,
    trim_training_readiness,
    trim_training_status,
    trim_vo2_max,
)
from tests.conftest import get_tool, load_fixture

# ---------------------------------------------------------------------------
# trim_training_status — drops, device-map collapse, allowlist canary
# ---------------------------------------------------------------------------


def test_trim_training_status_drops_top_level_noise() -> None:
    full = load_fixture("training_status_payload_full")
    trimmed = trim_training_status(full)

    # Top-level userId / heatAltitudeAcclimationDTO drop. `userId` drops via
    # the cross-cutting _strip_pii; `heatAltitudeAcclimationDTO` is dropped
    # explicitly because the populated copy lives at
    # `mostRecentVO2Max.heatAltitudeAcclimation`.
    assert "userId" not in trimmed
    assert "heatAltitudeAcclimationDTO" not in trimmed


def test_trim_training_status_drops_block_noise() -> None:
    full = load_fixture("training_status_payload_full")
    trimmed = trim_training_status(full)

    block = trimmed["mostRecentTrainingStatus"]
    # Cross-tool repeats / UI hints dropped.
    assert "recordedDevices" not in block
    assert "showSelector" not in block
    # Useful "data freshness" signal preserved.
    assert block["lastPrimarySyncDate"] == "2026-05-10"
    # PII inside the block dropped.
    assert "userId" not in block


def test_trim_training_status_collapses_single_device_map() -> None:
    """Single-device `{<deviceId>: {...}}` flattens to the inner dict."""
    full = load_fixture("training_status_payload_full")
    trimmed = trim_training_status(full)

    latest = trimmed["mostRecentTrainingStatus"]["latestTrainingStatusData"]
    # Collapsed: no longer keyed by deviceId.
    assert "3458499233" not in latest
    # Inner dict surfaced directly.
    assert latest["trainingStatus"] == 3
    assert latest["trainingStatusFeedbackPhrase"] == "PRODUCTIVE_CARDIO_3"
    assert latest["acuteTrainingLoadDTO"]["acwrPercent"] == 77

    load_map = trimmed["mostRecentTrainingLoadBalance"]["metricsTrainingLoadBalanceDTOMap"]
    assert "3458499233" not in load_map
    assert load_map["monthlyLoadAerobicLow"] == 412
    assert load_map["trainingBalanceFeedbackPhrase"] == "BALANCED_OPTIMAL"


def test_trim_training_status_keeps_multi_device_map() -> None:
    """Multi-device map preserved so the LLM can disambiguate watches."""
    payload = {
        "mostRecentTrainingStatus": {
            "latestTrainingStatusData": {
                "1111": {"trainingStatus": 3, "primaryTrainingDevice": True},
                "2222": {"trainingStatus": 1, "primaryTrainingDevice": False},
            },
        },
    }
    trimmed = trim_training_status(payload)
    latest = trimmed["mostRecentTrainingStatus"]["latestTrainingStatusData"]
    assert set(latest.keys()) == {"1111", "2222"}
    # Per-device noise still drops on multi-device path.
    for entry in latest.values():
        assert "primaryTrainingDevice" not in entry


def test_trim_training_status_drops_per_device_always_null_fields() -> None:
    full = load_fixture("training_status_payload_full")
    trimmed = trim_training_status(full)
    latest = trimmed["mostRecentTrainingStatus"]["latestTrainingStatusData"]
    # The four always-null device fields are dropped.
    for dropped in (
        "weeklyTrainingLoad",
        "loadTunnelMin",
        "loadTunnelMax",
        "loadLevelTrend",
        "primaryTrainingDevice",
        "deviceId",
    ):
        assert dropped not in latest


# Top-level keys we expect on a trimmed training-status. The trimmer keeps
# everything else not in a denylist, so a future Garmin field would
# pass through silently — this canary forces an explicit decision.
EXPECTED_TRAINING_STATUS_TOP_LEVEL_KEYS = frozenset(
    {
        "mostRecentVO2Max",
        "mostRecentTrainingLoadBalance",
        "mostRecentTrainingStatus",
    }
)


def test_trim_training_status_top_level_keys_subset_of_expected() -> None:
    full = load_fixture("training_status_payload_full")
    trimmed = trim_training_status(full)
    leaked = set(trimmed.keys()) - EXPECTED_TRAINING_STATUS_TOP_LEVEL_KEYS
    assert not leaked, (
        f"trim_training_status leaked unclassified fields: {sorted(leaked)}. "
        "Either add to EXPECTED_TRAINING_STATUS_TOP_LEVEL_KEYS or to the drop set."
    )


def test_trim_training_status_unknown_field_caught_by_canary() -> None:
    """Sanity: the canary actually flags an unknown top-level field."""
    full = dict(load_fixture("training_status_payload_full"))
    full["someNewBloatField"] = [1, 2, 3]
    trimmed = trim_training_status(full)
    leaked = set(trimmed.keys()) - EXPECTED_TRAINING_STATUS_TOP_LEVEL_KEYS
    assert leaked == {"someNewBloatField"}


def test_trim_training_status_passes_through_non_dict() -> None:
    assert trim_training_status(None) is None
    assert trim_training_status([]) == []
    assert trim_training_status("oops") == "oops"


def test_trim_training_status_passes_through_error_stub() -> None:
    stub = {"error": "garmin_unreachable", "message": "boom"}
    assert trim_training_status(stub) == stub


def test_trim_training_status_does_not_mutate_input() -> None:
    full = load_fixture("training_status_payload_full")
    snapshot = json.loads(json.dumps(full))
    trim_training_status(full)
    assert full == snapshot


# ---------------------------------------------------------------------------
# trim_training_load — load-balance only, no status / vo2 spillover
# ---------------------------------------------------------------------------


def test_trim_training_load_keeps_only_load_balance() -> None:
    full = load_fixture("training_status_payload_full")
    trimmed = trim_training_load(full)

    assert set(trimmed.keys()) == {"mostRecentTrainingLoadBalance"}
    # Specifically: status and vo2 are GONE (this is the API shape change).
    assert "mostRecentTrainingStatus" not in trimmed
    assert "mostRecentVO2Max" not in trimmed
    assert "heatAltitudeAcclimationDTO" not in trimmed


def test_trim_training_load_collapses_device_map() -> None:
    full = load_fixture("training_status_payload_full")
    trimmed = trim_training_load(full)
    inner = trimmed["mostRecentTrainingLoadBalance"]["metricsTrainingLoadBalanceDTOMap"]
    assert "3458499233" not in inner
    assert inner["monthlyLoadAerobicLow"] == 412
    assert inner["trainingBalanceFeedbackPhrase"] == "BALANCED_OPTIMAL"
    # Per-device noise dropped.
    assert "primaryTrainingDevice" not in inner
    assert "deviceId" not in inner


def test_trim_training_load_returns_empty_when_no_block() -> None:
    """Missing `mostRecentTrainingLoadBalance` yields a clean empty dict."""
    assert trim_training_load({}) == {}
    assert trim_training_load({"mostRecentTrainingLoadBalance": None}) == {}


def test_trim_training_load_passes_through_non_dict() -> None:
    assert trim_training_load(None) is None
    assert trim_training_load([]) == []


def test_trim_training_load_passes_through_error_stub() -> None:
    stub = {"error": "rate_limited", "message": "later"}
    assert trim_training_load(stub) == stub


# ---------------------------------------------------------------------------
# trim_training_readiness — factor envelope, most-recent default
# ---------------------------------------------------------------------------


def test_trim_training_readiness_default_returns_most_recent_dict() -> None:
    full = load_fixture("training_readiness_payload_full")
    trimmed = trim_training_readiness(full)

    # Single dict, not a list — this is the API shape change.
    assert isinstance(trimmed, dict)
    # Most-recent reading (timestamp 17:19:36) is first in the upstream.
    assert trimmed["timestamp"] == "2026-05-10T17:19:36.0"
    assert trimmed["score"] == 50
    assert trimmed["level"] == "MODERATE"
    assert trimmed["feedbackShort"] == "WELL_RESTED"


def test_trim_training_readiness_collapses_factor_pairs() -> None:
    full = load_fixture("training_readiness_payload_full")
    trimmed = trim_training_readiness(full)

    factors = trimmed["factors"]
    assert set(factors.keys()) == {
        "sleep",
        "recovery",
        "acwr",
        "stress",
        "hrv",
        "sleepHistory",
    }
    for envelope in factors.values():
        assert set(envelope.keys()) == {"score", "qualifier"}

    # Spot-check the actual values from the fixture's first reading.
    assert factors["sleep"] == {"score": 70, "qualifier": "GOOD"}
    assert factors["recovery"] == {"score": 76, "qualifier": "GOOD"}
    assert factors["acwr"] == {"score": 77, "qualifier": "GOOD"}
    assert factors["stress"] == {"score": 64, "qualifier": "MODERATE"}
    assert factors["hrv"] == {"score": 92, "qualifier": "GOOD"}
    assert factors["sleepHistory"] == {"score": 55, "qualifier": "MODERATE"}


def test_trim_training_readiness_drops_consumed_factor_fields() -> None:
    full = load_fixture("training_readiness_payload_full")
    trimmed = trim_training_readiness(full)
    # Every `*FactorPercent` / `*FactorFeedback` pair is consumed into
    # `factors` and dropped from the top level.
    for k in (
        "sleepScoreFactorPercent",
        "sleepScoreFactorFeedback",
        "recoveryTimeFactorPercent",
        "recoveryTimeFactorFeedback",
        "acwrFactorPercent",
        "acwrFactorFeedback",
        "stressHistoryFactorPercent",
        "stressHistoryFactorFeedback",
        "hrvFactorPercent",
        "hrvFactorFeedback",
        "sleepHistoryFactorPercent",
        "sleepHistoryFactorFeedback",
    ):
        assert k not in trimmed


def test_trim_training_readiness_renames_recovery_time_to_unit_clear() -> None:
    full = load_fixture("training_readiness_payload_full")
    trimmed = trim_training_readiness(full)
    assert "recoveryTime" not in trimmed
    assert trimmed["recoveryTime_min"] == 910


def test_trim_training_readiness_drops_noise_fields() -> None:
    full = load_fixture("training_readiness_payload_full")
    trimmed = trim_training_readiness(full)
    for k in (
        "deviceId",
        "feedbackLong",
        "validSleep",
        "inputContext",
        "primaryActivityTracker",
        "timestampLocal",
        "userProfilePK",
    ):
        assert k not in trimmed


def test_trim_training_readiness_keeps_overall_signals() -> None:
    full = load_fixture("training_readiness_payload_full")
    trimmed = trim_training_readiness(full)
    # The overall scalars survive (separate signal from per-factor envelopes).
    assert trimmed["sleepScore"] == 80
    assert trimmed["hrvWeeklyAverage"] == 50
    assert trimmed["acuteLoad"] == 360


def test_trim_training_readiness_drops_null_recovery_change_phrase() -> None:
    """First reading has null `recoveryTimeChangePhrase` → dropped."""
    full = load_fixture("training_readiness_payload_full")
    trimmed = trim_training_readiness(full)
    assert "recoveryTimeChangePhrase" not in trimmed


def test_trim_training_readiness_keeps_populated_recovery_change_phrase() -> None:
    """A non-null `recoveryTimeChangePhrase` survives — it's narrative signal."""
    full = load_fixture("training_readiness_payload_full")
    # Third reading carries a populated phrase. Force it as the most-recent
    # by reordering: pass the third reading alone.
    trimmed = trim_training_readiness([full[2]])
    assert trimmed["recoveryTimeChangePhrase"] == "RT_CHANGE_LARGE_NEG"


def test_trim_training_readiness_full_list_when_not_most_recent() -> None:
    full = load_fixture("training_readiness_payload_full")
    trimmed = trim_training_readiness(full, most_recent_only=False)
    assert isinstance(trimmed, list)
    assert len(trimmed) == 3
    for entry in trimmed:
        assert "factors" in entry
        assert "userProfilePK" not in entry


def test_trim_training_readiness_handles_empty_list() -> None:
    assert trim_training_readiness([]) == {}
    assert trim_training_readiness([], most_recent_only=False) == []


def test_trim_training_readiness_passes_through_non_list_non_dict() -> None:
    assert trim_training_readiness(None) is None
    assert trim_training_readiness("oops") == "oops"


def test_trim_training_readiness_error_stub_in_list_passes_through() -> None:
    """An error-stub element in a list is preserved in full-list mode."""
    stub = {"error": "rate_limited", "message": "back off"}
    out = trim_training_readiness([stub], most_recent_only=False)
    assert out == [stub]


def test_trim_training_readiness_error_stub_dict_passes_through() -> None:
    stub = {"error": "internal_error", "message": "boom"}
    assert trim_training_readiness(stub) == stub


def test_trim_training_readiness_does_not_mutate_input() -> None:
    full = load_fixture("training_readiness_payload_full")
    snapshot = json.loads(json.dumps(full))
    trim_training_readiness(full)
    assert full == snapshot


# ---------------------------------------------------------------------------
# trim_vo2_max — extracts from training-status, drops nulls
# ---------------------------------------------------------------------------


def test_trim_vo2_max_extracts_generic_block() -> None:
    full = load_fixture("training_status_payload_full")
    trimmed = trim_vo2_max(full)

    assert trimmed["calendarDate"] == "2026-05-03"
    assert trimmed["generic"]["vo2MaxValue"] == 49
    assert trimmed["generic"]["vo2MaxPreciseValue"] == 49.4
    assert trimmed["generic"]["maxMetCategory"] == 0


def test_trim_vo2_max_drops_null_fitness_age_fields() -> None:
    full = load_fixture("training_status_payload_full")
    trimmed = trim_vo2_max(full)
    assert "fitnessAge" not in trimmed["generic"]
    assert "fitnessAgeDescription" not in trimmed["generic"]


def test_trim_vo2_max_drops_heat_altitude_acclimation() -> None:
    """`heatAltitudeAcclimation` is a separate concern; lives in training_status."""
    full = load_fixture("training_status_payload_full")
    trimmed = trim_vo2_max(full)
    assert "heatAltitudeAcclimation" not in trimmed


def test_trim_vo2_max_drops_user_id() -> None:
    full = load_fixture("training_status_payload_full")
    trimmed = trim_vo2_max(full)
    assert "userId" not in trimmed
    assert "userId" not in trimmed.get("generic", {})


def test_trim_vo2_max_handles_null_cycling() -> None:
    """`cycling: null` upstream surfaces explicitly as `cycling: null`."""
    full = load_fixture("training_status_payload_full")
    trimmed = trim_vo2_max(full)
    assert trimmed["cycling"] is None


def test_trim_vo2_max_handles_populated_cycling() -> None:
    payload = {
        "mostRecentVO2Max": {
            "generic": {"calendarDate": "2026-05-03", "vo2MaxValue": 49, "vo2MaxPreciseValue": 49.4},
            "cycling": {"calendarDate": "2026-05-02", "vo2MaxValue": 42, "fitnessAge": None},
        }
    }
    trimmed = trim_vo2_max(payload)
    assert trimmed["cycling"]["vo2MaxValue"] == 42
    assert "fitnessAge" not in trimmed["cycling"]


def test_trim_vo2_max_returns_empty_when_block_missing() -> None:
    assert trim_vo2_max({}) == {}
    assert trim_vo2_max({"mostRecentVO2Max": None}) == {}


def test_trim_vo2_max_returns_empty_when_disciplines_null() -> None:
    payload = {"mostRecentVO2Max": {"generic": None, "cycling": None}}
    assert trim_vo2_max(payload) == {}


def test_trim_vo2_max_passes_through_non_dict() -> None:
    assert trim_vo2_max(None) is None
    assert trim_vo2_max([]) == []


def test_trim_vo2_max_passes_through_error_stub() -> None:
    stub = {"error": "garmin_unreachable", "message": "boom"}
    assert trim_vo2_max(stub) == stub


# ---------------------------------------------------------------------------
# _collapse_device_map helper — direct unit coverage
# ---------------------------------------------------------------------------


def test_collapse_device_map_single_device_returns_inner_dict() -> None:
    out = _collapse_device_map(
        {"3458499233": {"a": 1, "primaryTrainingDevice": True, "b": 2}},
        frozenset({"primaryTrainingDevice"}),
    )
    assert out == {"a": 1, "b": 2}


def test_collapse_device_map_multi_device_keeps_map() -> None:
    out = _collapse_device_map(
        {
            "1111": {"a": 1, "primaryTrainingDevice": True},
            "2222": {"a": 2, "primaryTrainingDevice": False},
        },
        frozenset({"primaryTrainingDevice"}),
    )
    assert out == {"1111": {"a": 1}, "2222": {"a": 2}}


def test_collapse_device_map_passes_through_non_dict() -> None:
    assert _collapse_device_map(None, frozenset()) is None
    assert _collapse_device_map([], frozenset()) == []


def test_collapse_device_map_handles_non_dict_entry() -> None:
    out = _collapse_device_map({"1111": "oops"}, frozenset())
    assert out == "oops"  # single-entry collapse surfaces the string


# ---------------------------------------------------------------------------
# Tool-level tests: cache, verbose, shape changes
# ---------------------------------------------------------------------------


def test_get_training_status_passes_date(mcp_with_tools, mock_garmin) -> None:
    fn = get_tool(mcp_with_tools, "get_training_status")
    fn(date="2026-05-09")
    mock_garmin.get_training_status.assert_called_once_with("2026-05-09")


def test_get_training_status_default_is_trimmed(mcp_with_tools, mock_garmin) -> None:
    full = load_fixture("training_status_payload_full")
    mock_garmin.get_training_status.return_value = full

    fn = get_tool(mcp_with_tools, "get_training_status")
    result = fn(date="2026-05-09")

    upstream = len(json.dumps(full))
    trimmed = len(json.dumps(result))
    assert trimmed < upstream
    # Sanity checks on shape.
    assert "userId" not in result
    assert "heatAltitudeAcclimationDTO" not in result
    block = result["mostRecentTrainingStatus"]
    assert "recordedDevices" not in block
    assert "showSelector" not in block
    assert "3458499233" not in block["latestTrainingStatusData"]


def test_get_training_status_verbose_returns_unmodified_upstream(
    mcp_with_tools, mock_garmin
) -> None:
    full = load_fixture("training_status_payload_full")
    mock_garmin.get_training_status.return_value = full
    fn = get_tool(mcp_with_tools, "get_training_status")
    assert fn(date="2026-05-09", verbose=True) == full


def test_get_training_status_cache_persists_full_upstream(
    mcp_with_tools, mock_garmin, monkeypatch
) -> None:
    """verbose=True after verbose=False hits the cache."""
    from garmin_mcp import cache

    monkeypatch.delenv("GARMIN_MCP_NO_CACHE", raising=False)
    cache.clear_all()

    full = load_fixture("training_status_payload_full")
    mock_garmin.get_training_status.return_value = full

    fn = get_tool(mcp_with_tools, "get_training_status")
    trimmed = fn(date="2026-05-09")
    verbose = fn(date="2026-05-09", verbose=True)

    assert mock_garmin.get_training_status.call_count == 1
    assert verbose == full
    assert "userId" not in trimmed
    assert "userId" in verbose

    cache.clear_all()


def test_get_training_load_default_is_load_balance_only(mcp_with_tools, mock_garmin) -> None:
    full = load_fixture("training_status_payload_full")
    mock_garmin.get_training_status.return_value = full

    fn = get_tool(mcp_with_tools, "get_training_load")
    result = fn(date="2026-05-09")

    # API shape change: status and vo2 are NO LONGER returned.
    assert set(result.keys()) == {"mostRecentTrainingLoadBalance"}
    inner = result["mostRecentTrainingLoadBalance"]["metricsTrainingLoadBalanceDTOMap"]
    assert inner["monthlyLoadAerobicLow"] == 412


def test_get_training_load_verbose_returns_full_status(mcp_with_tools, mock_garmin) -> None:
    full = load_fixture("training_status_payload_full")
    mock_garmin.get_training_status.return_value = full
    fn = get_tool(mcp_with_tools, "get_training_load")
    assert fn(date="2026-05-09", verbose=True) == full


def test_get_training_load_handles_non_dict(mcp_with_tools, mock_garmin) -> None:
    mock_garmin.get_training_status.return_value = []
    fn = get_tool(mcp_with_tools, "get_training_load")
    # Empty upstream falls back to {} after the `or {}` in _fetch_training_status
    # → trim_training_load on {} returns {}.
    assert fn(date="2026-05-09") == {}


def test_get_training_load_shares_cache_with_status(
    mcp_with_tools, mock_garmin, monkeypatch
) -> None:
    """Calling get_training_status then get_training_load triggers ONE Garmin call."""
    from garmin_mcp import cache

    monkeypatch.delenv("GARMIN_MCP_NO_CACHE", raising=False)
    cache.clear_all()

    full = load_fixture("training_status_payload_full")
    mock_garmin.get_training_status.return_value = full

    status_fn = get_tool(mcp_with_tools, "get_training_status")
    load_fn = get_tool(mcp_with_tools, "get_training_load")

    status_fn(date="2026-05-09")
    load_fn(date="2026-05-09")
    assert mock_garmin.get_training_status.call_count == 1

    cache.clear_all()


def test_get_vo2_max_sources_from_training_status(mcp_with_tools, mock_garmin) -> None:
    """`get_vo2_max` calls the training-status endpoint, not legacy max_metrics."""
    full = load_fixture("training_status_payload_full")
    mock_garmin.get_training_status.return_value = full

    fn = get_tool(mcp_with_tools, "get_vo2_max")
    result = fn(date="2026-05-09")

    mock_garmin.get_training_status.assert_called_once_with("2026-05-09")
    # Legacy endpoint must NOT be called — that's the bug fix.
    mock_garmin.get_max_metrics.assert_not_called()
    assert result["calendarDate"] == "2026-05-03"
    assert result["generic"]["vo2MaxValue"] == 49


def test_get_vo2_max_shares_cache_with_status(
    mcp_with_tools, mock_garmin, monkeypatch
) -> None:
    """status → vo2 → load: three tool calls, exactly one Garmin hit."""
    from garmin_mcp import cache

    monkeypatch.delenv("GARMIN_MCP_NO_CACHE", raising=False)
    cache.clear_all()

    full = load_fixture("training_status_payload_full")
    mock_garmin.get_training_status.return_value = full

    status_fn = get_tool(mcp_with_tools, "get_training_status")
    vo2_fn = get_tool(mcp_with_tools, "get_vo2_max")
    load_fn = get_tool(mcp_with_tools, "get_training_load")

    status_fn(date="2026-05-09")
    vo2_fn(date="2026-05-09")
    load_fn(date="2026-05-09")

    assert mock_garmin.get_training_status.call_count == 1

    cache.clear_all()


def test_get_vo2_max_handles_missing_data_cleanly(mcp_with_tools, mock_garmin) -> None:
    """When upstream has no VO2 data, return {} rather than crashing."""
    payload = {"mostRecentVO2Max": None}
    mock_garmin.get_training_status.return_value = payload
    fn = get_tool(mcp_with_tools, "get_vo2_max")
    assert fn(date="2026-05-09") == {}


def test_get_vo2_max_verbose_returns_full_training_status(
    mcp_with_tools, mock_garmin
) -> None:
    full = load_fixture("training_status_payload_full")
    mock_garmin.get_training_status.return_value = full
    fn = get_tool(mcp_with_tools, "get_vo2_max")
    assert fn(date="2026-05-09", verbose=True) == full


def test_get_training_readiness_default_returns_most_recent(
    mcp_with_tools, mock_garmin
) -> None:
    full = load_fixture("training_readiness_payload_full")
    mock_garmin.get_training_readiness.return_value = full

    fn = get_tool(mcp_with_tools, "get_training_readiness")
    result = fn(date="2026-05-10")

    # API shape change: single dict, not a list.
    assert isinstance(result, dict)
    assert result["score"] == 50
    assert result["timestamp"] == "2026-05-10T17:19:36.0"
    assert "factors" in result


def test_get_training_readiness_verbose_returns_full_list(
    mcp_with_tools, mock_garmin
) -> None:
    full = load_fixture("training_readiness_payload_full")
    mock_garmin.get_training_readiness.return_value = full
    fn = get_tool(mcp_with_tools, "get_training_readiness")
    result = fn(date="2026-05-10", verbose=True)
    assert isinstance(result, list)
    assert len(result) == 3
    assert result == full


def test_get_training_readiness_cache_persists_full_upstream(
    mcp_with_tools, mock_garmin, monkeypatch
) -> None:
    from garmin_mcp import cache

    monkeypatch.delenv("GARMIN_MCP_NO_CACHE", raising=False)
    cache.clear_all()

    full = load_fixture("training_readiness_payload_full")
    mock_garmin.get_training_readiness.return_value = full

    fn = get_tool(mcp_with_tools, "get_training_readiness")
    trimmed = fn(date="2026-05-10")
    verbose = fn(date="2026-05-10", verbose=True)

    assert mock_garmin.get_training_readiness.call_count == 1
    assert verbose == full
    assert isinstance(trimmed, dict)
    assert isinstance(verbose, list)

    cache.clear_all()


def test_get_training_readiness_handles_empty_upstream(mcp_with_tools, mock_garmin) -> None:
    mock_garmin.get_training_readiness.return_value = []
    fn = get_tool(mcp_with_tools, "get_training_readiness")
    assert fn(date="2026-05-10") == {}


def test_get_training_readiness_handles_none_upstream(mcp_with_tools, mock_garmin) -> None:
    mock_garmin.get_training_readiness.return_value = None
    fn = get_tool(mcp_with_tools, "get_training_readiness")
    assert fn(date="2026-05-10") == {}


def test_get_race_predictor(mcp_with_tools, mock_garmin) -> None:
    fn = get_tool(mcp_with_tools, "get_race_predictor")
    result = fn()
    assert result["time5K"] == 1180


# ---------------------------------------------------------------------------
# Cross-cutting PII strip — every new helper applies _strip_pii
# ---------------------------------------------------------------------------


_CROSS_CUTTING_PII_KEYS = {
    "userProfileId",
    "userId",
    "userProfilePK",
    "userProfilePk",
    "displayName",
    "displayname",
    "fullName",
    "fullname",
    "ownerDisplayName",
    "ownerFullName",
    "ownerId",
    "imageURL",
    "imageUrl",
    "userPro",
}


def _walk_keys(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from _walk_keys(v)
    elif isinstance(obj, list):
        for item in obj:
            yield from _walk_keys(item)


@pytest.mark.parametrize(
    "helper,fixture",
    [
        (trim_training_status, "training_status_payload_full"),
        (trim_training_load, "training_status_payload_full"),
        (trim_vo2_max, "training_status_payload_full"),
        (trim_training_readiness, "training_readiness_payload_full"),
    ],
)
def test_training_helpers_strip_cross_cutting_fields(helper, fixture) -> None:
    full = copy.deepcopy(load_fixture(fixture))
    trimmed = helper(full)
    for key in _walk_keys(trimmed):
        assert key not in _CROSS_CUTTING_PII_KEYS, (
            f"{helper.__name__} leaked cross-cutting PII key {key!r}"
        )
        assert not (
            isinstance(key, str)
            and (key.endswith("TimeLocal") or key.endswith("TimestampLocal"))
        ), f"{helper.__name__} leaked `*Local` timestamp key {key!r}"
