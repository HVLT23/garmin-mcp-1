"""Shared pytest fixtures."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from mcp.server.fastmcp import FastMCP

from garmin_mcp import cache
from garmin_mcp.tools import register_all

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> Any:
    """Load a JSON fixture by base filename (without .json)."""
    return json.loads((FIXTURES / f"{name}.json").read_text())


@pytest.fixture(autouse=True)
def _no_cache(monkeypatch):
    """Disable in-process caching so tests don't bleed across each other."""
    monkeypatch.setenv("GARMIN_MCP_NO_CACHE", "1")
    cache.clear_all()
    yield
    cache.clear_all()


@pytest.fixture
def mock_garmin() -> MagicMock:
    """A MagicMock with the subset of Garmin methods we use, returning fixtures."""
    m = MagicMock()
    m.get_activities.return_value = load_fixture("activities_list")
    m.get_activity.return_value = load_fixture("activity")
    m.get_activity_splits.return_value = load_fixture("activity_splits")
    m.get_activity_hr_in_timezones.return_value = load_fixture("activity_hr_zones")
    m.get_activities_by_date.return_value = load_fixture("activities_by_date")

    m.get_sleep_data.return_value = load_fixture("sleep")
    m.get_hrv_data.return_value = load_fixture("hrv")
    m.get_body_battery.return_value = load_fixture("body_battery")
    m.get_stress_data.return_value = load_fixture("stress")
    m.get_steps_data.return_value = load_fixture("steps")
    m.get_user_summary.return_value = load_fixture("daily_summary")

    m.get_training_status.return_value = load_fixture("training_status")
    m.get_training_readiness.return_value = load_fixture("training_readiness")
    m.get_max_metrics.return_value = load_fixture("max_metrics")
    m.get_race_predictions.return_value = load_fixture("race_predictions")

    m.upload_running_workout.return_value = load_fixture("workout_upload_response")
    # Live Garmin response uses bare `id` (verified against the calendar
    # listing). Tests cover the `scheduledWorkoutId` and `workoutScheduleId`
    # fallbacks separately.
    m.schedule_workout.return_value = {"id": 111222333}
    m.unschedule_workout.return_value = {"status": "ok"}
    m.delete_workout.return_value = {"status": "ok"}
    m.get_scheduled_workouts.return_value = load_fixture("scheduled_workouts")

    m.get_full_name.return_value = "Test User"
    return m


@pytest.fixture
def mcp_with_tools(mock_garmin) -> FastMCP:
    """A FastMCP instance with all tools registered against the mocked client."""
    mcp = FastMCP("garmin-mcp-test")
    register_all(mcp, lambda: mock_garmin)
    return mcp


def get_tool(mcp: FastMCP, name: str):
    """Return the underlying Python callable for a registered tool."""
    return mcp._tool_manager._tools[name].fn
