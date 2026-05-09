"""Tests for the activities tool module."""

from __future__ import annotations

from tests.conftest import get_tool


def test_list_recent_activities_returns_list(mcp_with_tools, mock_garmin) -> None:
    fn = get_tool(mcp_with_tools, "list_recent_activities")
    result = fn(limit=5, start=0)
    assert isinstance(result, list)
    assert result[0]["activityId"] == 9999000001
    mock_garmin.get_activities.assert_called_once_with(0, 5)


def test_list_recent_activities_normalises_dict(mcp_with_tools, mock_garmin) -> None:
    mock_garmin.get_activities.return_value = {"activityList": [{"activityId": 1}]}
    fn = get_tool(mcp_with_tools, "list_recent_activities")
    result = fn()
    assert result == [{"activityId": 1}]


def test_get_activity_passes_string_id(mcp_with_tools, mock_garmin) -> None:
    fn = get_tool(mcp_with_tools, "get_activity")
    result = fn(activity_id=9999000001)
    assert result["activityId"] == 9999000001
    mock_garmin.get_activity.assert_called_once_with("9999000001")


def test_get_activity_splits(mcp_with_tools, mock_garmin) -> None:
    fn = get_tool(mcp_with_tools, "get_activity_splits")
    result = fn(activity_id=9999000001)
    assert "lapDTOs" in result
    mock_garmin.get_activity_splits.assert_called_once_with("9999000001")


def test_get_activity_hr_zones(mcp_with_tools, mock_garmin) -> None:
    fn = get_tool(mcp_with_tools, "get_activity_hr_zones")
    result = fn(activity_id=9999000001)
    assert isinstance(result, list)
    mock_garmin.get_activity_hr_in_timezones.assert_called_once_with("9999000001")


def test_search_activities_by_type_passes_dates(mcp_with_tools, mock_garmin) -> None:
    fn = get_tool(mcp_with_tools, "search_activities_by_type")
    result = fn(type="running", start_date="2026-05-01", end_date="2026-05-08", limit=2)
    assert len(result) == 2
    mock_garmin.get_activities_by_date.assert_called_once_with(
        "2026-05-01", "2026-05-08", activitytype="running"
    )


def test_search_activities_by_type_rejects_bad_date(mcp_with_tools) -> None:
    fn = get_tool(mcp_with_tools, "search_activities_by_type")
    result = fn(type="running", start_date="not-a-date", end_date="2026-05-08")
    # safe_call wraps the ValueError into a structured error
    assert isinstance(result, dict)
    assert result.get("error") == "internal_error"


def test_auth_error_returns_structured_payload(mock_garmin, mcp_with_tools) -> None:
    from garmin_mcp.auth import AuthError

    mock_garmin.get_activity.side_effect = AuthError("expired")
    fn = get_tool(mcp_with_tools, "get_activity")
    result = fn(activity_id=1)
    assert result == {
        "error": "auth_expired",
        "message": "expired",
        "remediation": "run `garmin-mcp auth login`",
    }
