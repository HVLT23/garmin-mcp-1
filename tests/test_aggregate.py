"""Tests for aggregate.get_session_with_context — including prior-night date math."""

from __future__ import annotations

import datetime as dt

import pytest

from garmin_mcp.tools.aggregate import _parse_activity_start, prior_night_date
from tests.conftest import get_tool


@pytest.mark.parametrize(
    "start, expected",
    [
        # Late-evening: prior night = the night ending the morning OF activity_date.
        (dt.datetime(2026, 5, 9, 23, 0), dt.date(2026, 5, 9)),
        # Mid-day: same as above.
        (dt.datetime(2026, 5, 9, 18, 0), dt.date(2026, 5, 9)),
        # Right at the 04:00 boundary — not "before".
        (dt.datetime(2026, 5, 9, 4, 0), dt.date(2026, 5, 9)),
        # Just before 04:00 → prior night = night ending yesterday morning.
        (dt.datetime(2026, 5, 9, 3, 59), dt.date(2026, 5, 8)),
        # Midnight → still "before 04:00", so prior night = day before.
        (dt.datetime(2026, 5, 9, 0, 0), dt.date(2026, 5, 8)),
        # Year boundary: 00:30 on Jan 1 → prior night = Dec 31 of previous year.
        (dt.datetime(2026, 1, 1, 0, 30), dt.date(2025, 12, 31)),
        # Year boundary, late evening: 23:00 on Dec 31 → prior night = Dec 31.
        (dt.datetime(2025, 12, 31, 23, 0), dt.date(2025, 12, 31)),
    ],
)
def test_prior_night_date(start, expected) -> None:
    assert prior_night_date(start) == expected


def test_parse_activity_start_space_format() -> None:
    assert _parse_activity_start({"startTimeLocal": "2026-05-09 07:30:00"}) == dt.datetime(
        2026, 5, 9, 7, 30
    )


def test_parse_activity_start_iso_format() -> None:
    assert _parse_activity_start({"startTimeLocal": "2026-05-09T07:30:00"}) == dt.datetime(
        2026, 5, 9, 7, 30
    )


def test_parse_activity_start_in_summary_dto() -> None:
    payload = {"summaryDTO": {"startTimeLocal": "2026-05-09 07:30:00"}}
    assert _parse_activity_start(payload) == dt.datetime(2026, 5, 9, 7, 30)


def test_parse_activity_start_missing_returns_none() -> None:
    assert _parse_activity_start({}) is None


def test_parse_activity_start_unparseable_returns_none() -> None:
    assert _parse_activity_start({"startTimeLocal": "yesterday"}) is None


def test_get_session_with_context_full_bundle(mcp_with_tools, mock_garmin) -> None:
    """Activity at 07:30 → prior night = same date; bundle has every section."""
    fn = get_tool(mcp_with_tools, "get_session_with_context")
    result = fn(activity_id=9999000001)

    assert "activity" in result
    assert "splits" in result
    assert "hr_zones" in result
    assert "prior_night_sleep" in result
    assert "prior_day_hrv" in result
    assert "morning_body_battery" in result
    assert "morning_readiness" in result
    assert "training_load_at_time" in result

    # Date math: activity at 07:30 → sleep_date = activity_date.
    dates = result["context_dates"]
    assert dates["activity_date"] == "2026-05-09"
    assert dates["sleep_date"] == "2026-05-09"

    # Underlying calls used the right dates.
    mock_garmin.get_sleep_data.assert_called_once_with("2026-05-09")
    mock_garmin.get_hrv_data.assert_called_once_with("2026-05-09")
    mock_garmin.get_body_battery.assert_called_once_with("2026-05-09", "2026-05-09")
    mock_garmin.get_training_readiness.assert_called_once_with("2026-05-09")
    mock_garmin.get_training_status.assert_called_once_with("2026-05-09")


def test_get_session_with_context_pre_4am(mcp_with_tools, mock_garmin) -> None:
    """Activity at 03:00 on May 9 → prior night = May 8."""
    activity = {
        "activityId": 1,
        "startTimeLocal": "2026-05-09 03:00:00",
    }
    mock_garmin.get_activity.return_value = activity

    fn = get_tool(mcp_with_tools, "get_session_with_context")
    result = fn(activity_id=1)

    dates = result["context_dates"]
    assert dates["activity_date"] == "2026-05-09"
    assert dates["sleep_date"] == "2026-05-08"
    mock_garmin.get_sleep_data.assert_called_once_with("2026-05-08")


def test_get_session_with_context_missing_start_time(mcp_with_tools, mock_garmin) -> None:
    mock_garmin.get_activity.return_value = {"activityId": 1}
    fn = get_tool(mcp_with_tools, "get_session_with_context")
    result = fn(activity_id=1)
    assert result["context"]["error"] == "missing_start_time"
    # We still return splits and hr_zones in the degraded response.
    assert "splits" in result
    assert "hr_zones" in result


def test_get_session_with_context_partial_failure(mcp_with_tools, mock_garmin) -> None:
    """One sub-call failing should not poison the whole bundle."""
    mock_garmin.get_hrv_data.side_effect = RuntimeError("boom")
    fn = get_tool(mcp_with_tools, "get_session_with_context")
    result = fn(activity_id=9999000001)
    assert result["prior_day_hrv"]["error"] == "fetch_failed"
    assert result["prior_night_sleep"]["dailySleepDTO"]["calendarDate"] == "2026-05-09"


def test_get_session_with_context_bad_activity_payload(mcp_with_tools, mock_garmin) -> None:
    mock_garmin.get_activity.return_value = "not a dict"
    fn = get_tool(mcp_with_tools, "get_session_with_context")
    result = fn(activity_id=1)
    assert result == {
        "error": "invalid_activity",
        "message": "activity 1 returned non-dict",
    }


def test_aggregate_does_not_cache_partial_failure(
    mcp_with_tools, mock_garmin, monkeypatch
) -> None:
    """A transient sub-call failure must NOT be cached as a stub.

    Regression test for the round-2 review M1 finding: previously,
    `@cached(ttl=TTL_ACTIVITY_FINAL)` on the outer aggregate stored the
    `{"error": "fetch_failed", ...}` stub for 24h. After the per-section
    refactor, sub-call errors are returned but never cached.
    """
    from garmin_mcp import cache
    from tests.conftest import load_fixture

    # Disable the autouse no-cache override so we actually exercise the
    # cache path.
    monkeypatch.delenv("GARMIN_MCP_NO_CACHE", raising=False)
    cache.clear_all()

    # First call: HRV raises → error stub in the bundle.
    mock_garmin.get_hrv_data.side_effect = RuntimeError("transient")
    fn = get_tool(mcp_with_tools, "get_session_with_context")
    r1 = fn(activity_id=9999000001)
    assert r1["prior_day_hrv"]["error"] == "fetch_failed"

    # Recover. Same caller, same activity_id, same sleep_date — but the
    # error must NOT have been cached, so this call should re-fetch HRV.
    mock_garmin.get_hrv_data.side_effect = None
    mock_garmin.get_hrv_data.return_value = load_fixture("hrv")

    r2 = fn(activity_id=9999000001)
    assert "error" not in r2["prior_day_hrv"]
    assert r2["prior_day_hrv"]["hrvSummary"]["status"] == "BALANCED"

    # Sanity: HRV was hit twice (transient error + recovery), but the
    # successfully-fetched sections were cached after the first call so
    # they shouldn't have been re-fetched.
    assert mock_garmin.get_hrv_data.call_count == 2
    assert mock_garmin.get_sleep_data.call_count == 1
    assert mock_garmin.get_training_readiness.call_count == 1

    cache.clear_all()
