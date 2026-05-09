"""Tests for wellness tools — date defaulting, return shapes."""

from __future__ import annotations

import datetime as dt

from tests.conftest import get_tool


def test_get_sleep_uses_today_when_no_date(mcp_with_tools, mock_garmin) -> None:
    fn = get_tool(mcp_with_tools, "get_sleep")
    fn()
    args = mock_garmin.get_sleep_data.call_args.args
    assert args[0] == dt.date.today().isoformat()


def test_get_sleep_passes_explicit_date(mcp_with_tools, mock_garmin) -> None:
    fn = get_tool(mcp_with_tools, "get_sleep")
    fn(date="2026-05-08")
    mock_garmin.get_sleep_data.assert_called_once_with("2026-05-08")


def test_get_hrv_returns_empty_dict_when_none(mcp_with_tools, mock_garmin) -> None:
    mock_garmin.get_hrv_data.return_value = None
    fn = get_tool(mcp_with_tools, "get_hrv")
    assert fn(date="2026-05-08") == {}


def test_get_body_battery_defaults_end_to_start(mcp_with_tools, mock_garmin) -> None:
    fn = get_tool(mcp_with_tools, "get_body_battery")
    fn(start_date="2026-05-01")
    mock_garmin.get_body_battery.assert_called_once_with("2026-05-01", "2026-05-01")


def test_get_body_battery_explicit_end(mcp_with_tools, mock_garmin) -> None:
    fn = get_tool(mcp_with_tools, "get_body_battery")
    fn(start_date="2026-05-01", end_date="2026-05-05")
    mock_garmin.get_body_battery.assert_called_once_with("2026-05-01", "2026-05-05")


def test_get_stress(mcp_with_tools, mock_garmin) -> None:
    fn = get_tool(mcp_with_tools, "get_stress")
    result = fn(date="2026-05-09")
    assert result["calendarDate"] == "2026-05-09"


def test_get_steps_returns_list(mcp_with_tools, mock_garmin) -> None:
    fn = get_tool(mcp_with_tools, "get_steps")
    assert isinstance(fn(date="2026-05-09"), list)


def test_get_daily_summary(mcp_with_tools, mock_garmin) -> None:
    fn = get_tool(mcp_with_tools, "get_daily_summary")
    result = fn(date="2026-05-09")
    assert result["totalSteps"] == 12500


def test_bad_iso_date_returns_error(mcp_with_tools) -> None:
    fn = get_tool(mcp_with_tools, "get_sleep")
    result = fn(date="2026/05/09")
    assert isinstance(result, dict)
    assert result.get("error") == "internal_error"
