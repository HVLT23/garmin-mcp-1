"""The aggregate `get_session_with_context` tool — combines a single activity
with the wellness/training signals the LLM needs to reason about it.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Callable
from typing import Any

from garminconnect import Garmin
from mcp.server.fastmcp import FastMCP

from garmin_mcp import cache
from garmin_mcp.cache import (
    TTL_ACTIVITY_FINAL,
    TTL_TRAINING_STATUS,
    TTL_WELLNESS,
    _current_user_id,
)
from garmin_mcp.tools._helpers import audited, safe_call

logger = logging.getLogger(__name__)

ClientFactory = Callable[[], Garmin]


def _parse_activity_start(activity: dict[str, Any]) -> dt.datetime | None:
    """Extract the activity's local start time. Returns None if missing."""
    raw = activity.get("startTimeLocal") or activity.get("summaryDTO", {}).get("startTimeLocal")
    if not raw:
        return None
    # Garmin formats are typically "YYYY-MM-DD HH:MM:SS" or ISO with 'T'.
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f"):
        try:
            return dt.datetime.strptime(raw, fmt)
        except ValueError:
            continue
    try:
        return dt.datetime.fromisoformat(raw)
    except ValueError:
        return None


def prior_night_date(activity_start: dt.datetime) -> dt.date:
    """Compute the date Garmin records the "prior night" sleep under.

    Garmin records each sleep block under the date the user wakes up.
    Convention from the brief:
      - activity starts before 04:00 → prior night = the night ending the
        morning before the activity (sleep recorded under activity_date - 1).
      - otherwise → prior night = the night ending the morning OF the
        activity (sleep recorded under activity_date).
    """
    activity_date = activity_start.date()
    if activity_start.hour < 4:
        return activity_date - dt.timedelta(days=1)
    return activity_date


def _capture(
    ttl: int,
    key: tuple[Any, ...],
    label: str,
    fn: Callable[[], Any],
) -> Any:
    """Fetch a single section of the aggregate.

    Each section gets its own TTL bucket so a failure on one signal doesn't
    poison the whole bundle: successes are cached at `ttl`, errors are
    swallowed into a `{"error": "fetch_failed", ...}` stub but **never
    cached** — so a transient blip clears as soon as the upstream recovers
    instead of pinning a stale stub for the duration of the longest TTL.
    """
    if cache._disabled():
        try:
            return fn()
        except Exception as e:
            logger.warning("aggregate sub-call %s failed: %s", label, e)
            return {"error": "fetch_failed", "message": str(e), "section": label}

    cache_obj, lock = cache._cache_for(ttl)
    cache_key: tuple[Any, ...] = (_current_user_id(), label, *key)
    with lock:
        if cache_key in cache_obj:
            return cache_obj[cache_key]
    try:
        result = fn()
    except Exception as e:
        logger.warning("aggregate sub-call %s failed: %s", label, e)
        return {"error": "fetch_failed", "message": str(e), "section": label}
    with lock:
        cache_obj[cache_key] = result
    return result


def register(mcp: FastMCP, client_factory: ClientFactory) -> None:
    @mcp.tool()
    @audited
    @safe_call
    def get_session_with_context(activity_id: int) -> dict[str, Any]:
        """One-shot bundle: activity + the daily-life signals around it.

        Returns: activity, splits, hr_zones, prior_night_sleep,
        prior_day_hrv, morning_body_battery, morning_readiness,
        training_load_at_time.
        """
        client = client_factory()
        aid = str(activity_id)

        # Primary fetch isn't routed through `_capture` — if Garmin refuses,
        # `safe_call` translates the raised exception into a structured error
        # at the tool boundary. (Caching happens via the standalone
        # `get_activity` tool if the user calls that separately.)
        activity = client.get_activity(aid)
        if not isinstance(activity, dict):
            return {"error": "invalid_activity", "message": f"activity {aid} returned non-dict"}

        splits = _capture(
            TTL_ACTIVITY_FINAL, ("splits", aid), "splits",
            lambda: client.get_activity_splits(aid),
        )
        hr_zones = _capture(
            TTL_ACTIVITY_FINAL, ("hr_zones", aid), "hr_zones",
            lambda: client.get_activity_hr_in_timezones(aid),
        )

        start = _parse_activity_start(activity)
        if start is None:
            return {
                "activity": activity,
                "splits": splits,
                "hr_zones": hr_zones,
                "context": {
                    "error": "missing_start_time",
                    "message": "activity has no startTimeLocal — cannot align context windows",
                },
            }

        sleep_date = prior_night_date(start).isoformat()
        activity_date = start.date().isoformat()

        return {
            "activity": activity,
            "splits": splits,
            "hr_zones": hr_zones,
            "prior_night_sleep": _capture(
                TTL_WELLNESS, ("sleep", sleep_date), "prior_night_sleep",
                lambda: client.get_sleep_data(sleep_date),
            ),
            "prior_day_hrv": _capture(
                TTL_WELLNESS, ("hrv", sleep_date), "prior_day_hrv",
                lambda: client.get_hrv_data(sleep_date),
            ),
            "morning_body_battery": _capture(
                TTL_WELLNESS, ("body_battery", activity_date), "morning_body_battery",
                lambda: client.get_body_battery(activity_date, activity_date),
            ),
            "morning_readiness": _capture(
                TTL_TRAINING_STATUS, ("readiness", activity_date), "morning_readiness",
                lambda: client.get_training_readiness(activity_date),
            ),
            "training_load_at_time": _capture(
                TTL_TRAINING_STATUS, ("training_status", activity_date), "training_load_at_time",
                lambda: client.get_training_status(activity_date),
            ),
            "context_dates": {
                "activity_start_local": start.isoformat(),
                "sleep_date": sleep_date,
                "activity_date": activity_date,
            },
        }
