"""Live integration smoke tests.

Skipped unless GARMIN_LIVE_TEST=1. Requires already-persisted tokens at the
default path (run `garmin-mcp auth login` once before).
"""

from __future__ import annotations

import os

import pytest

from garmin_mcp.auth import load_client
from garmin_mcp.config import load_settings

pytestmark = pytest.mark.skipif(
    os.environ.get("GARMIN_LIVE_TEST") != "1",
    reason="set GARMIN_LIVE_TEST=1 to run live integration tests",
)


@pytest.fixture(scope="module")
def live_client():
    settings = load_settings()
    return load_client(settings.garmin_tokens_path)


def test_list_recent_activities_live(live_client) -> None:
    result = live_client.get_activities(0, 5)
    assert result is not None


def test_daily_summary_live(live_client) -> None:
    import datetime as dt
    today = dt.date.today().isoformat()
    summary = live_client.get_user_summary(today)
    assert isinstance(summary, dict)
