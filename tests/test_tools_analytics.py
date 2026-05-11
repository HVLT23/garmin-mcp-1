"""Tests for analytics tools — trend, activity summary, baseline compare, today summary."""

from __future__ import annotations

import datetime as dt
from unittest.mock import MagicMock

import pytest
from mcp.server.fastmcp import FastMCP

from garmin_mcp import cache
from garmin_mcp.tools import register_all
from garmin_mcp.tools.analytics import (
    _linreg_slope,
    _percentiles,
    _qualifier,
    _qualify_trend,
)
from tests.conftest import get_tool

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

def _trend_mcp(per_date_payloads: dict[str, dict], endpoint: str = "get_hrv_data"):
    """Build an MCP wired to a mock that dispatches per-date payloads.

    `per_date_payloads` maps an ISO date string to the dict the endpoint
    should return for that date. Dates not in the map raise — exposing
    test bugs where a tool reaches outside the planned window.
    """
    mock = MagicMock()
    method = getattr(mock, endpoint)

    def _dispatch(date, *args, **kwargs):
        if date in per_date_payloads:
            return per_date_payloads[date]
        raise KeyError(f"unexpected date {date!r}")

    method.side_effect = _dispatch
    mcp = FastMCP("test")
    register_all(mcp, lambda: mock)
    return mcp, mock


def _hrv(value: int) -> dict:
    return {"hrvSummary": {"lastNightAvg": value}}


def _daily(**fields) -> dict:
    return {"calendarDate": "2026-05-09", **fields}


# ---------------------------------------------------------------------------
# Pure-function helpers
# ---------------------------------------------------------------------------

def test_linreg_slope_positive() -> None:
    assert _linreg_slope([1.0, 2.0, 3.0, 4.0]) == pytest.approx(1.0)


def test_linreg_slope_negative() -> None:
    assert _linreg_slope([10.0, 8.0, 6.0, 4.0]) == pytest.approx(-2.0)


def test_linreg_slope_flat() -> None:
    assert _linreg_slope([5.0, 5.0, 5.0]) == 0.0


def test_linreg_slope_single_returns_zero() -> None:
    assert _linreg_slope([7.0]) == 0.0


def test_qualify_trend_rising() -> None:
    rising = [float(i) for i in range(1, 15)]
    slope = _linreg_slope(rising)
    assert _qualify_trend(rising, slope) == "rising"


def test_qualify_trend_falling() -> None:
    falling = [float(15 - i) for i in range(14)]
    slope = _linreg_slope(falling)
    assert _qualify_trend(falling, slope) == "falling"


def test_qualify_trend_flat_is_stable() -> None:
    flat = [50.0] * 10
    assert _qualify_trend(flat, _linreg_slope(flat)) == "stable"


def test_qualify_trend_noisy_is_stable() -> None:
    """Noise around a mean with no clear direction should bucket as stable."""
    noisy = [50, 55, 48, 52, 49, 53, 50, 51, 49, 50, 52, 48, 51, 50]
    f = [float(v) for v in noisy]
    assert _qualify_trend(f, _linreg_slope(f)) == "stable"


def test_qualifier_buckets() -> None:
    assert _qualifier(-15.0) == "low"
    assert _qualifier(-5.0) == "typical"
    assert _qualifier(0.0) == "typical"
    assert _qualifier(10.0) == "typical"
    assert _qualifier(15.0) == "elevated"
    assert _qualifier(None) is None


def test_percentiles_minmax() -> None:
    out = _percentiles([float(v) for v in range(1, 11)], with_minmax=True)
    assert out["min"] == 1.0
    assert out["max"] == 10.0
    assert out["p50"] == 5.5
    # statistics.quantiles for 10 evenly-spaced values: deciles[8] (90th boundary) = 9.9
    assert out["p90"] == pytest.approx(9.9)


def test_percentiles_single_sample() -> None:
    out = _percentiles([42.0])
    assert out == {"p50": 42.0, "p90": 42.0}


# ---------------------------------------------------------------------------
# Tool 1: get_metric_trend
# ---------------------------------------------------------------------------

def test_metric_trend_rising_hrv() -> None:
    """A strictly rising HRV sequence reports trend=rising and the right deltas."""
    anchor = dt.date(2026, 5, 14)
    payloads = {
        (anchor - dt.timedelta(days=offset)).isoformat(): _hrv(50 + (13 - offset))
        for offset in range(14)
    }
    mcp, _ = _trend_mcp(payloads, "get_hrv_data")
    fn = get_tool(mcp, "get_metric_trend")
    r = fn(metric="hrv_overnight", days=14, anchor_date="2026-05-14")

    assert r["metric"] == "hrv_overnight"
    assert r["window_days"] == 14
    assert r["n_samples"] == 14
    assert r["missing_dates"] == []
    assert r["today"] == 63
    assert r["yesterday"] == 62
    assert r["trend"] == "rising"
    assert r["trend_slope_per_day"] == pytest.approx(1.0)
    assert r["delta_vs_yesterday"] == 1
    assert r["min"] == 50
    assert r["max"] == 63
    # First sample chronological, last sample = today.
    assert r["samples"][0]["date"] == "2026-05-01"
    assert r["samples"][-1]["date"] == "2026-05-14"
    # 14d default → monthly_avg suppressed.
    assert r["monthly_avg"] is None


def test_metric_trend_falling_sleep_score() -> None:
    anchor = dt.date(2026, 5, 14)
    payloads = {
        (anchor - dt.timedelta(days=offset)).isoformat(): {
            "dailySleepDTO": {"sleepScores": {"overall": {"value": 60 + offset}}}
        }
        for offset in range(14)
    }
    mcp, _ = _trend_mcp(payloads, "get_sleep_data")
    fn = get_tool(mcp, "get_metric_trend")
    r = fn(metric="sleep_score", days=14, anchor_date="2026-05-14")
    assert r["trend"] == "falling"
    assert r["today"] == 60
    assert r["yesterday"] == 61


def test_metric_trend_flat_is_stable() -> None:
    anchor = dt.date(2026, 5, 14)
    payloads = {
        (anchor - dt.timedelta(days=offset)).isoformat(): _hrv(50)
        for offset in range(14)
    }
    mcp, _ = _trend_mcp(payloads, "get_hrv_data")
    fn = get_tool(mcp, "get_metric_trend")
    r = fn(metric="hrv_overnight", days=14, anchor_date="2026-05-14")
    assert r["trend"] == "stable"
    assert r["delta_vs_yesterday"] == 0
    assert r["delta_vs_yesterday_pct"] == 0.0


def test_metric_trend_noisy_is_stable() -> None:
    anchor = dt.date(2026, 5, 14)
    noise = [50, 55, 48, 52, 49, 53, 50, 51, 49, 50, 52, 48, 51, 50]
    payloads = {
        (anchor - dt.timedelta(days=13 - i)).isoformat(): _hrv(noise[i])
        for i in range(14)
    }
    mcp, _ = _trend_mcp(payloads, "get_hrv_data")
    fn = get_tool(mcp, "get_metric_trend")
    r = fn(metric="hrv_overnight", days=14, anchor_date="2026-05-14")
    assert r["trend"] == "stable"


def test_metric_trend_handles_missing_dates() -> None:
    """3 of 14 days missing → samples & missing_dates report it; trend still computed."""
    anchor = dt.date(2026, 5, 14)
    missing_offsets = {2, 7, 11}
    payloads = {}
    for offset in range(14):
        date = (anchor - dt.timedelta(days=offset)).isoformat()
        if offset in missing_offsets:
            payloads[date] = {}  # empty payload → extractor returns None
        else:
            payloads[date] = _hrv(50)
    mcp, _ = _trend_mcp(payloads, "get_hrv_data")
    fn = get_tool(mcp, "get_metric_trend")
    r = fn(metric="hrv_overnight", days=14, anchor_date="2026-05-14")

    assert r["n_samples"] == 11
    assert len(r["missing_dates"]) == 3
    expected_missing = {(anchor - dt.timedelta(days=o)).isoformat() for o in missing_offsets}
    assert set(r["missing_dates"]) == expected_missing
    # Trend is computable from the 11 surviving samples.
    assert r["trend"] == "stable"


def test_metric_trend_insufficient_data() -> None:
    """When more than half the window is missing, return insufficient_data error."""
    anchor = dt.date(2026, 5, 14)
    # Only 1 of 14 days populated.
    payloads = {}
    for offset in range(14):
        date = (anchor - dt.timedelta(days=offset)).isoformat()
        payloads[date] = _hrv(50) if offset == 0 else {}
    mcp, _ = _trend_mcp(payloads, "get_hrv_data")
    fn = get_tool(mcp, "get_metric_trend")
    r = fn(metric="hrv_overnight", days=14, anchor_date="2026-05-14")

    assert r["error"] == "insufficient_data"
    assert r["n_samples"] == 1
    assert len(r["missing_dates"]) == 13
    assert "samples" in r  # partial sample list still surfaced


def test_metric_trend_unknown_metric_returns_bad_argument() -> None:
    mcp = FastMCP("test")
    register_all(mcp, lambda: MagicMock())
    fn = get_tool(mcp, "get_metric_trend")
    r = fn(metric="bogus", days=14)
    assert r["error"] == "bad_argument"
    assert "supported" in r["message"]


def test_metric_trend_default_anchor_is_today(monkeypatch) -> None:
    """anchor_date=None resolves to today.isoformat()."""
    today = dt.date.today()
    payloads = {
        (today - dt.timedelta(days=offset)).isoformat(): _hrv(50 + offset)
        for offset in range(14)
    }
    mcp, _ = _trend_mcp(payloads, "get_hrv_data")
    fn = get_tool(mcp, "get_metric_trend")
    r = fn(metric="hrv_overnight", days=14)  # no anchor_date
    assert r["anchor_date"] == today.isoformat()


def test_metric_trend_load_path(mock_garmin) -> None:
    """acute_training_load resolves through the multi-device-or-collapsed path."""
    # Build a per-date dispatcher for training_status.
    anchor = dt.date(2026, 5, 14)
    def make_status(value: float) -> dict:
        return {
            "mostRecentTrainingStatus": {
                "latestTrainingStatusData": {
                    "3458499233": {
                        "acuteTrainingLoadDTO": {
                            "dailyTrainingLoadAcute": value,
                            "dailyTrainingLoadChronic": 380.0,
                        }
                    }
                }
            }
        }

    payloads = {
        (anchor - dt.timedelta(days=offset)).isoformat(): make_status(300 + offset)
        for offset in range(14)
    }
    mcp, _ = _trend_mcp(payloads, "get_training_status")
    fn = get_tool(mcp, "get_metric_trend")
    r = fn(metric="acute_training_load", days=14, anchor_date="2026-05-14")
    assert r["n_samples"] == 14
    assert r["today"] == 300  # offset=0
    assert r["yesterday"] == 301


def test_metric_trend_monthly_avg_when_30_days() -> None:
    anchor = dt.date(2026, 5, 30)
    payloads = {
        (anchor - dt.timedelta(days=offset)).isoformat(): _hrv(50)
        for offset in range(30)
    }
    mcp, _ = _trend_mcp(payloads, "get_hrv_data")
    fn = get_tool(mcp, "get_metric_trend")
    r = fn(metric="hrv_overnight", days=30, anchor_date="2026-05-30")
    assert r["monthly_avg"] == 50.0


# ---------------------------------------------------------------------------
# Tool 2: get_recent_activity_summary
# ---------------------------------------------------------------------------

def _activity(
    *,
    aid: int,
    type_key: str,
    start: str,
    duration: float = 1800.0,
    distance: float = 5000.0,
    avg_hr: float = 140.0,
    calories: float = 500.0,
) -> dict:
    return {
        "activityId": aid,
        "activityType": {"typeKey": type_key},
        "startTimeLocal": start,
        "duration": duration,
        "distance": distance,
        "averageHR": avg_hr,
        "calories": calories,
    }


def test_activity_summary_filters_by_type() -> None:
    activities = [
        _activity(aid=1, type_key="running", start="2026-05-13 07:00:00"),
        _activity(aid=2, type_key="cycling", start="2026-05-12 18:00:00"),
        _activity(aid=3, type_key="running", start="2026-05-10 07:00:00"),
        _activity(aid=4, type_key="strength_training", start="2026-05-09 19:00:00",
                  distance=0.0),
    ]
    mock = MagicMock()
    mock.get_activities.return_value = activities
    mcp = FastMCP("test")
    register_all(mcp, lambda: mock)
    fn = get_tool(mcp, "get_recent_activity_summary")
    r = fn(activity_type="running", days=14, anchor_date="2026-05-14")

    assert r["activity_type"] == "running"
    assert r["n_activities"] == 2
    # types_seen only emitted when activity_type=None.
    assert "types_seen" not in r


def test_activity_summary_no_filter_emits_types_seen() -> None:
    activities = [
        _activity(aid=1, type_key="running", start="2026-05-13 07:00:00"),
        _activity(aid=2, type_key="cycling", start="2026-05-12 18:00:00"),
        _activity(aid=3, type_key="running", start="2026-05-10 07:00:00"),
    ]
    mock = MagicMock()
    mock.get_activities.return_value = activities
    mcp = FastMCP("test")
    register_all(mcp, lambda: mock)
    fn = get_tool(mcp, "get_recent_activity_summary")
    r = fn(days=14, anchor_date="2026-05-14")

    assert r["n_activities"] == 3
    assert r["types_seen"] == {"running": 2, "cycling": 1}


def test_activity_summary_skips_zero_distance() -> None:
    """Strength sessions with distance=0 don't pollute distance_km stats."""
    activities = [
        _activity(aid=1, type_key="strength_training", start="2026-05-13 07:00:00",
                  distance=0.0),
        _activity(aid=2, type_key="strength_training", start="2026-05-11 07:00:00",
                  distance=0.0),
    ]
    mock = MagicMock()
    mock.get_activities.return_value = activities
    mcp = FastMCP("test")
    register_all(mcp, lambda: mock)
    fn = get_tool(mcp, "get_recent_activity_summary")
    r = fn(days=14, anchor_date="2026-05-14")

    assert r["total_distance_km"] == 0
    assert "distance_km" not in r  # block omitted when no samples


def test_activity_summary_window_excludes_outside_dates() -> None:
    activities = [
        _activity(aid=1, type_key="running", start="2026-05-13 07:00:00"),
        _activity(aid=2, type_key="running", start="2026-04-20 07:00:00"),  # outside
        _activity(aid=3, type_key="running", start="2026-06-01 07:00:00"),  # future
    ]
    mock = MagicMock()
    mock.get_activities.return_value = activities
    mcp = FastMCP("test")
    register_all(mcp, lambda: mock)
    fn = get_tool(mcp, "get_recent_activity_summary")
    r = fn(days=14, anchor_date="2026-05-14")
    assert r["n_activities"] == 1


def test_activity_summary_empty_window() -> None:
    mock = MagicMock()
    mock.get_activities.return_value = []
    mcp = FastMCP("test")
    register_all(mcp, lambda: mock)
    fn = get_tool(mcp, "get_recent_activity_summary")
    r = fn(days=14, anchor_date="2026-05-14")
    assert r["n_activities"] == 0
    assert r["total_calories"] == 0
    assert r["activities_per_week"] == 0


# ---------------------------------------------------------------------------
# Tool 3: compare_activity_to_baseline
# ---------------------------------------------------------------------------

def _full_activity(
    *,
    aid: int,
    type_key: str,
    start: str,
    duration: float,
    avg_hr: float,
    calories: float,
    training_load: float,
    **extras,
) -> dict:
    return {
        "activityId": aid,
        "activityType": {"typeKey": type_key},
        "startTimeLocal": start,
        "duration": duration,
        "averageHR": avg_hr,
        "calories": calories,
        "activityTrainingLoad": training_load,
        **extras,
    }


def test_compare_baseline_happy_path() -> None:
    target = _full_activity(
        aid=200, type_key="boxing", start="2026-05-14 09:00:00",
        duration=60 * 86, avg_hr=150, calories=900, training_load=120.0,
    )
    baseline = [
        _full_activity(aid=101, type_key="boxing", start="2026-05-12 09:00:00",
                       duration=60 * 80, avg_hr=140, calories=820, training_load=100.0),
        _full_activity(aid=102, type_key="boxing", start="2026-05-09 09:00:00",
                       duration=60 * 78, avg_hr=138, calories=810, training_load=95.0),
        _full_activity(aid=103, type_key="boxing", start="2026-05-06 09:00:00",
                       duration=60 * 76, avg_hr=136, calories=800, training_load=90.0),
        _full_activity(aid=104, type_key="boxing", start="2026-05-03 09:00:00",
                       duration=60 * 74, avg_hr=134, calories=790, training_load=85.0),
    ]
    mock = MagicMock()
    mock.get_activity.return_value = target
    # get_activities returns newest-first; include target + baseline.
    mock.get_activities.return_value = [target, *baseline]
    mcp = FastMCP("test")
    register_all(mcp, lambda: mock)
    fn = get_tool(mcp, "compare_activity_to_baseline")
    r = fn(activity_id=200, baseline_days=14)

    assert r["activity_type"] == "boxing"
    assert r["baseline_n"] == 4
    assert r["baseline_insufficient"] is False
    assert set(r["baseline_dates"]) == {
        "2026-05-12", "2026-05-09", "2026-05-06", "2026-05-03"
    }

    hr = r["comparisons"]["averageHR"]
    assert hr["today"] == 150
    assert hr["baseline_p50"] == 137  # median of [140, 138, 136, 134]
    assert hr["delta"] == 13
    # 13/137 ≈ 9.49% → within ±10% threshold → "typical"
    expected_pct = round((150 - 137) / 137 * 100, 1)
    assert hr["delta_pct"] == expected_pct
    assert hr["qualifier"] == "typical"

    cal = r["comparisons"]["calories"]
    # baseline median = median([820, 810, 800, 790]) = 805
    assert cal["baseline_p50"] == 805
    assert cal["delta"] == 95
    assert cal["qualifier"] == "elevated"

    tl = r["comparisons"]["trainingLoad"]
    # baseline median([100, 95, 90, 85]) = 92.5
    assert tl["baseline_p50"] == 92.5
    assert tl["qualifier"] == "elevated"


def test_compare_baseline_insufficient() -> None:
    target = _full_activity(
        aid=300, type_key="boxing", start="2026-05-14 09:00:00",
        duration=60 * 86, avg_hr=150, calories=900, training_load=120.0,
    )
    # Only 2 baseline activities — below the 3 threshold.
    baseline = [
        _full_activity(aid=201, type_key="boxing", start="2026-05-12 09:00:00",
                       duration=60 * 80, avg_hr=140, calories=820, training_load=100.0),
        _full_activity(aid=202, type_key="boxing", start="2026-05-09 09:00:00",
                       duration=60 * 78, avg_hr=138, calories=810, training_load=95.0),
    ]
    mock = MagicMock()
    mock.get_activity.return_value = target
    mock.get_activities.return_value = [target, *baseline]
    mcp = FastMCP("test")
    register_all(mcp, lambda: mock)
    fn = get_tool(mcp, "compare_activity_to_baseline")
    r = fn(activity_id=300, baseline_days=14)

    assert r["baseline_insufficient"] is True
    assert r["baseline_n"] == 2
    assert r["comparisons"] == {}
    assert "note" in r


def test_compare_baseline_running_includes_run_specific_fields() -> None:
    """Running activities pull in cadence / vertical ratio / ground contact / stride."""
    target = _full_activity(
        aid=400, type_key="running", start="2026-05-14 07:00:00",
        duration=60 * 35, avg_hr=155, calories=400, training_load=60.0,
        averageRunningCadenceInStepsPerMinute=180.0,
        avgVerticalRatio=8.0,
        avgGroundContactTime=240.0,
        avgStrideLength=1.20,
    )
    baseline = [
        _full_activity(aid=301 + i, type_key="running",
                       start=f"2026-05-{10+i} 07:00:00",
                       duration=60 * 30, avg_hr=150, calories=380, training_load=55.0,
                       averageRunningCadenceInStepsPerMinute=178.0,
                       avgVerticalRatio=8.5,
                       avgGroundContactTime=245.0,
                       avgStrideLength=1.15)
        for i in range(3)
    ]
    mock = MagicMock()
    mock.get_activity.return_value = target
    mock.get_activities.return_value = [target, *baseline]
    mcp = FastMCP("test")
    register_all(mcp, lambda: mock)
    fn = get_tool(mcp, "compare_activity_to_baseline")
    r = fn(activity_id=400, baseline_days=14)

    assert "averageRunningCadenceInStepsPerMinute" in r["comparisons"]
    assert "avgVerticalRatio" in r["comparisons"]
    assert "avgGroundContactTime" in r["comparisons"]
    assert "avgStrideLength" in r["comparisons"]


def test_compare_baseline_non_running_skips_running_fields() -> None:
    target = _full_activity(
        aid=500, type_key="boxing", start="2026-05-14 09:00:00",
        duration=60 * 86, avg_hr=150, calories=900, training_load=120.0,
    )
    baseline = [
        _full_activity(aid=410 + i, type_key="boxing",
                       start=f"2026-05-{i+5} 09:00:00",
                       duration=60 * 80, avg_hr=140, calories=820, training_load=100.0)
        for i in range(3)
    ]
    mock = MagicMock()
    mock.get_activity.return_value = target
    mock.get_activities.return_value = [target, *baseline]
    mcp = FastMCP("test")
    register_all(mcp, lambda: mock)
    fn = get_tool(mcp, "compare_activity_to_baseline")
    r = fn(activity_id=500, baseline_days=14)
    assert "averageRunningCadenceInStepsPerMinute" not in r["comparisons"]


def test_compare_baseline_invalid_activity() -> None:
    mock = MagicMock()
    mock.get_activity.return_value = "not a dict"
    mcp = FastMCP("test")
    register_all(mcp, lambda: mock)
    fn = get_tool(mcp, "compare_activity_to_baseline")
    r = fn(activity_id=1)
    assert r["error"] == "invalid_activity"


def test_compare_baseline_missing_start_time() -> None:
    mock = MagicMock()
    mock.get_activity.return_value = {
        "activityId": 1,
        "activityType": {"typeKey": "running"},
    }
    mcp = FastMCP("test")
    register_all(mcp, lambda: mock)
    fn = get_tool(mcp, "compare_activity_to_baseline")
    r = fn(activity_id=1)
    assert r["error"] == "missing_start_time"


# ---------------------------------------------------------------------------
# Tool 4: get_today_summary
# ---------------------------------------------------------------------------

def test_today_summary_full(mcp_with_tools, mock_garmin) -> None:
    fn = get_tool(mcp_with_tools, "get_today_summary")
    r = fn(date="2026-05-09")
    for key in ("sleep", "hrv", "body_battery", "training_readiness",
                "training_status", "daily_summary"):
        assert key in r, f"section {key} missing"
    assert r["date"] == "2026-05-09"
    mock_garmin.get_sleep_data.assert_called_with("2026-05-09")
    mock_garmin.get_hrv_data.assert_called_with("2026-05-09")
    mock_garmin.get_body_battery.assert_called_with("2026-05-09", "2026-05-09")


def test_today_summary_partial_failure(mcp_with_tools, mock_garmin) -> None:
    """One section failing must NOT poison the whole bundle."""
    mock_garmin.get_hrv_data.side_effect = RuntimeError("boom")
    fn = get_tool(mcp_with_tools, "get_today_summary")
    r = fn(date="2026-05-09")
    assert r["hrv"]["error"] == "fetch_failed"
    # Unaffected sections still carry data.
    assert "calendarDate" in r["daily_summary"] or "totalSteps" in r["daily_summary"]


def test_today_summary_default_date_is_today(mcp_with_tools, mock_garmin) -> None:
    fn = get_tool(mcp_with_tools, "get_today_summary")
    fn()
    today = dt.date.today().isoformat()
    mock_garmin.get_sleep_data.assert_called_with(today)


# ---------------------------------------------------------------------------
# Cache reuse + error-stub passthrough
# ---------------------------------------------------------------------------

def test_metric_trend_cache_reuse(monkeypatch) -> None:
    """A second call within TTL should hit the cache for previously fetched dates."""
    anchor = dt.date(2026, 5, 14)
    payloads = {
        (anchor - dt.timedelta(days=offset)).isoformat(): _hrv(50 + offset)
        for offset in range(14)
    }
    mock = MagicMock()

    def _dispatch(date, *args, **kwargs):
        return payloads[date]
    mock.get_hrv_data.side_effect = _dispatch
    mcp = FastMCP("test")
    register_all(mcp, lambda: mock)
    fn = get_tool(mcp, "get_metric_trend")

    # Enable caching for this test (the autouse fixture disables it).
    monkeypatch.delenv("GARMIN_MCP_NO_CACHE", raising=False)
    cache.clear_all()

    fn(metric="hrv_overnight", days=14, anchor_date="2026-05-14")
    first_count = mock.get_hrv_data.call_count
    assert first_count == 14

    # Second call → cache covers the same 14 dates.
    fn(metric="hrv_overnight", days=14, anchor_date="2026-05-14")
    assert mock.get_hrv_data.call_count == first_count

    cache.clear_all()


def test_today_summary_does_not_cache_partial_failure(monkeypatch) -> None:
    """Per-section error stubs must not be cached (regression of the aggregate fix)."""
    from tests.conftest import load_fixture

    mock = MagicMock()
    mock.get_sleep_data.return_value = load_fixture("sleep")
    mock.get_hrv_data.side_effect = RuntimeError("transient")
    mock.get_body_battery.return_value = load_fixture("body_battery")
    mock.get_training_readiness.return_value = load_fixture("training_readiness")
    mock.get_training_status.return_value = load_fixture("training_status")
    mock.get_user_summary.return_value = load_fixture("daily_summary")

    mcp = FastMCP("test")
    register_all(mcp, lambda: mock)
    fn = get_tool(mcp, "get_today_summary")

    monkeypatch.delenv("GARMIN_MCP_NO_CACHE", raising=False)
    cache.clear_all()

    r1 = fn(date="2026-05-09")
    assert r1["hrv"]["error"] == "fetch_failed"

    mock.get_hrv_data.side_effect = None
    mock.get_hrv_data.return_value = load_fixture("hrv")
    r2 = fn(date="2026-05-09")
    assert "error" not in r2["hrv"]
    # HRV was re-fetched; sleep was cached after the first success.
    assert mock.get_hrv_data.call_count == 2
    assert mock.get_sleep_data.call_count == 1

    cache.clear_all()


def test_metric_trend_bad_iso_date_returns_bad_argument(mcp_with_tools) -> None:
    fn = get_tool(mcp_with_tools, "get_metric_trend")
    r = fn(metric="hrv_overnight", days=14, anchor_date="2026/05/09")
    assert r["error"] == "bad_argument"
