"""Tests for training tools."""

from __future__ import annotations

from garmin_mcp.tools.training import _extract_training_load
from tests.conftest import get_tool


def test_get_training_status_passes_date(mcp_with_tools, mock_garmin) -> None:
    fn = get_tool(mcp_with_tools, "get_training_status")
    fn(date="2026-05-09")
    mock_garmin.get_training_status.assert_called_once_with("2026-05-09")


def test_get_training_readiness(mcp_with_tools, mock_garmin) -> None:
    fn = get_tool(mcp_with_tools, "get_training_readiness")
    result = fn(date="2026-05-09")
    assert result[0]["score"] == 82


def test_get_vo2_max(mcp_with_tools, mock_garmin) -> None:
    fn = get_tool(mcp_with_tools, "get_vo2_max")
    fn(date="2026-05-09")
    mock_garmin.get_max_metrics.assert_called_once_with("2026-05-09")


def test_get_training_load_distills(mcp_with_tools, mock_garmin) -> None:
    fn = get_tool(mcp_with_tools, "get_training_load")
    result = fn(date="2026-05-09")
    assert "mostRecentTrainingLoadBalance" in result
    assert "loadTunnel" in result
    assert "userId" not in result  # sanity: untouched fields filtered out


def test_get_training_load_handles_non_dict(mcp_with_tools, mock_garmin) -> None:
    mock_garmin.get_training_status.return_value = []
    fn = get_tool(mcp_with_tools, "get_training_load")
    assert fn(date="2026-05-09") == {}


def test_get_race_predictor(mcp_with_tools, mock_garmin) -> None:
    fn = get_tool(mcp_with_tools, "get_race_predictor")
    result = fn()
    assert result["time5K"] == 1180


def test_extract_training_load_picks_known_keys() -> None:
    payload = {
        "mostRecentTrainingStatus": {"a": 1},
        "mostRecentTrainingLoadBalance": {"b": 2},
        "mostRecentVO2Max": {"c": 3},
        "loadTunnel": {"d": 4},
        "unrelated": "ignored",
    }
    out = _extract_training_load(payload)
    assert set(out) == {
        "mostRecentTrainingStatus",
        "mostRecentTrainingLoadBalance",
        "mostRecentVO2Max",
        "loadTunnel",
    }


def test_extract_training_load_handles_empty() -> None:
    assert _extract_training_load({}) == {}
