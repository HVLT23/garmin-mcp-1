"""Tests for the `whoami` identity tool."""

from __future__ import annotations

from unittest.mock import MagicMock

from mcp.server.fastmcp import FastMCP

from garmin_mcp.registry import CURRENT_USER, LEGACY_USER_ID
from garmin_mcp.tools import register_all
from tests.conftest import get_tool


def _whoami_mcp(mock):
    mcp = FastMCP("test")
    register_all(mcp, lambda: mock)
    return mcp


def _mock(
    *,
    full_name: str | None = "Kasia Doe",
    user_profile: dict | None = None,
    activities: list | None = None,
) -> MagicMock:
    m = MagicMock()
    m.get_full_name.return_value = full_name
    m.get_user_profile.return_value = user_profile or {}
    # The whoami tool reads the most-recent activity via `get_activities(0, 1)`
    # — matching the live SDK's signature.
    m.get_activities.return_value = activities if activities is not None else []
    return m


def test_whoami_happy_path() -> None:
    """All four fields populated when every upstream succeeds."""
    activity = {
        "activityId": 1,
        "startTimeLocal": "2026-05-11 14:00:00",
        "startTimeGMT": "2026-05-11 12:00:00",
        "timeZoneId": "Europe/Berlin",
    }
    mock = _mock(
        full_name="Kasia Doe",
        user_profile={"userData": {"timeZone": "Europe/Berlin"}},
        activities=[activity],
    )
    mcp = _whoami_mcp(mock)
    fn = get_tool(mcp, "whoami")

    token = CURRENT_USER.set("kasia")
    try:
        result = fn()
    finally:
        CURRENT_USER.reset(token)

    assert result == {
        "user_id": "kasia",
        "display_name": "Kasia Doe",
        "timezone": "Europe/Berlin",
        "timezone_offset_min": 120,
    }


def test_whoami_missing_display_name_returns_null_field() -> None:
    """get_full_name failing must not poison the other fields."""
    mock = _mock(activities=[{
        "startTimeLocal": "2026-05-11 14:00:00",
        "startTimeGMT": "2026-05-11 12:00:00",
        "timeZoneId": "Europe/Berlin",
    }])
    mock.get_full_name.side_effect = RuntimeError("profile unreachable")
    mock.get_user_profile.return_value = {"userData": {"timeZone": "Europe/Berlin"}}
    fn = get_tool(_whoami_mcp(mock), "whoami")

    result = fn()

    assert result["display_name"] is None
    assert result["timezone"] == "Europe/Berlin"
    assert result["timezone_offset_min"] == 120
    assert result["user_id"] == LEGACY_USER_ID


def test_whoami_no_recent_activities_yields_null_offset() -> None:
    """An empty activity list → offset is null; other fields survive."""
    mock = _mock(
        full_name="Kasia Doe",
        user_profile={"userData": {"timeZone": "Europe/Berlin"}},
        activities=[],
    )
    fn = get_tool(_whoami_mcp(mock), "whoami")

    result = fn()

    assert result["display_name"] == "Kasia Doe"
    assert result["timezone"] == "Europe/Berlin"
    assert result["timezone_offset_min"] is None


def test_whoami_caches_per_user(monkeypatch) -> None:
    """Within a TTL window each upstream is hit exactly once per user."""
    from garmin_mcp import cache

    monkeypatch.delenv("GARMIN_MCP_NO_CACHE", raising=False)
    cache.clear_all()

    activity = {
        "startTimeLocal": "2026-05-11 14:00:00",
        "startTimeGMT": "2026-05-11 12:00:00",
        "timeZoneId": "Europe/Berlin",
    }
    mock = _mock(
        full_name="Kasia Doe",
        user_profile={"userData": {"timeZone": "Europe/Berlin"}},
        activities=[activity],
    )
    fn = get_tool(_whoami_mcp(mock), "whoami")

    token = CURRENT_USER.set("kasia")
    try:
        first = fn()
        second = fn()
    finally:
        CURRENT_USER.reset(token)
        cache.clear_all()

    assert first == second
    assert mock.get_full_name.call_count == 1
    assert mock.get_user_profile.call_count == 1
    # Both the timezone-fallback path and the offset path read the
    # recent activity. They share the same cached fetcher, so the
    # upstream Garmin call should happen exactly once.
    assert mock.get_activities.call_count == 1


def test_whoami_falls_back_to_legacy_user_id_when_contextvar_unset() -> None:
    """Stdio / no-middleware paths report `LEGACY_USER_ID` rather than failing."""
    mock = _mock(activities=[])
    fn = get_tool(_whoami_mcp(mock), "whoami")

    assert CURRENT_USER.get() is None
    result = fn()
    assert result["user_id"] == LEGACY_USER_ID


def test_whoami_activity_timezone_fallback_when_profile_lacks_it() -> None:
    """If user-settings has no TZ, use the most-recent activity's string TZ."""
    mock = _mock(
        full_name="Kasia Doe",
        user_profile={"userData": {}},
        activities=[{
            "startTimeLocal": "2026-05-11 14:00:00",
            "startTimeGMT": "2026-05-11 12:00:00",
            "timeZoneId": "America/Chicago",
        }],
    )
    fn = get_tool(_whoami_mcp(mock), "whoami")
    result = fn()
    assert result["timezone"] == "America/Chicago"


def test_whoami_ignores_numeric_timezone_id() -> None:
    """Numeric `timeZoneId` (internal Garmin ID) is not an IANA name — drop it."""
    mock = _mock(
        full_name=None,
        user_profile={},
        activities=[{
            "startTimeLocal": "2026-05-11 14:00:00",
            "startTimeGMT": "2026-05-11 12:00:00",
            "timeZoneId": 124,
        }],
    )
    fn = get_tool(_whoami_mcp(mock), "whoami")
    result = fn()
    assert result["timezone"] is None
    # The offset still comes from the local/GMT delta.
    assert result["timezone_offset_min"] == 120


def test_whoami_negative_offset() -> None:
    """West-of-UTC users get a negative offset."""
    mock = _mock(
        activities=[{
            "startTimeLocal": "2026-05-11 06:00:00",
            "startTimeGMT": "2026-05-11 12:00:00",
        }],
    )
    fn = get_tool(_whoami_mcp(mock), "whoami")
    result = fn()
    assert result["timezone_offset_min"] == -360


def test_whoami_safe_call_wraps_top_level_errors(monkeypatch) -> None:
    """If the tool body itself blows up, `safe_call` produces an error stub."""
    fn = get_tool(_whoami_mcp(_mock()), "whoami")

    # Force the contextvar read to raise so the failure happens before
    # any per-field try/except can catch it.
    def boom(*_a, **_kw):
        raise RuntimeError("contextvar exploded")

    monkeypatch.setattr(
        "garmin_mcp.tools.identity.CURRENT_USER",
        MagicMock(get=boom),
    )

    result = fn()
    assert result == {"error": "internal_error", "message": "contextvar exploded"}


def test_whoami_handles_unparseable_timestamps() -> None:
    """A malformed startTimeLocal yields a null offset rather than crashing."""
    mock = _mock(
        activities=[{
            "startTimeLocal": "not a timestamp",
            "startTimeGMT": "2026-05-11 12:00:00",
        }],
    )
    fn = get_tool(_whoami_mcp(mock), "whoami")
    result = fn()
    assert result["timezone_offset_min"] is None
