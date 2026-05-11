"""Identity MCP tool — single deliberate surface for who the caller is.

Every other tool's response goes through `_strip_pii` (cross-cutting PR
#17), so the LLM has no other channel to discover whose data it's
analysing. `whoami` is the one tool that *intentionally* returns
identity, scoped to the authenticated user only.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any

from garminconnect import Garmin
from mcp.server.fastmcp import FastMCP

from garmin_mcp.cache import TTL_PROFILE, cached
from garmin_mcp.registry import CURRENT_USER, LEGACY_USER_ID
from garmin_mcp.tools._helpers import audited, safe_call

logger = logging.getLogger(__name__)

ClientFactory = Callable[[], Garmin]


def register(mcp: FastMCP, client_factory: ClientFactory) -> None:
    # Each lookup is cached independently so a transient failure on one
    # field (e.g. the user-settings endpoint returning empty) doesn't
    # poison the cache for the others. The cache key is user-namespaced
    # in `_make_key`, so two tenants never share a cached identity.
    @cached(ttl=TTL_PROFILE)
    def _fetch_full_name() -> str | None:
        return client_factory().get_full_name()

    @cached(ttl=TTL_PROFILE)
    def _fetch_user_profile() -> dict[str, Any]:
        return client_factory().get_user_profile() or {}

    @cached(ttl=TTL_PROFILE)
    def _fetch_most_recent_activity() -> dict[str, Any] | None:
        result = client_factory().get_activities(0, 1)
        items = result.get("activityList", []) if isinstance(result, dict) else (result or [])
        return items[0] if items else None

    def _resolve_display_name() -> str | None:
        try:
            name = _fetch_full_name()
        except Exception:
            logger.debug("whoami: full_name lookup failed", exc_info=True)
            return None
        if isinstance(name, str) and name.strip():
            return name
        return None

    def _resolve_timezone() -> str | None:
        try:
            profile = _fetch_user_profile()
        except Exception:
            logger.debug("whoami: user_profile lookup failed", exc_info=True)
            profile = {}
        # The user-settings endpoint nests its payload under `userData`;
        # check both the nested and top-level forms because Garmin has
        # been inconsistent across endpoint versions.
        for candidate in _timezone_candidates(profile):
            if isinstance(candidate, str) and candidate.strip():
                return candidate
        try:
            activity = _fetch_most_recent_activity()
        except Exception:
            logger.debug("whoami: activity lookup failed", exc_info=True)
            return None
        if isinstance(activity, dict):
            tz = activity.get("timeZoneId")
            # Pass through only string values — Garmin sometimes returns
            # a numeric internal ID here, which isn't an IANA name and
            # would mislead the caller.
            if isinstance(tz, str) and tz.strip():
                return tz
        return None

    def _resolve_offset_min() -> int | None:
        try:
            activity = _fetch_most_recent_activity()
        except Exception:
            logger.debug("whoami: activity lookup failed", exc_info=True)
            return None
        if not isinstance(activity, dict):
            return None
        local = activity.get("startTimeLocal")
        gmt = activity.get("startTimeGMT")
        if not (isinstance(local, str) and isinstance(gmt, str)):
            return None
        try:
            local_dt = datetime.fromisoformat(local)
            gmt_dt = datetime.fromisoformat(gmt)
        except ValueError:
            logger.debug("whoami: failed to parse activity timestamps", exc_info=True)
            return None
        delta_seconds = (local_dt - gmt_dt).total_seconds()
        return round(delta_seconds / 60)

    @mcp.tool()
    @audited
    @safe_call
    def whoami() -> dict[str, Any]:
        """Return the authenticated user's identity + timezone.

        Use this at the start of a session to address the user by name
        and to anchor "today" / "yesterday" in their local timezone.
        Every other tool strips PII (the cross-cutting `_strip_pii`
        pass), so this is the only tool that returns identity at all —
        scoped to the authenticated user.

        Returns a dict with four keys:

          - `user_id`: the registry's user_id for the request's bearer
            (or `_legacy` in single-tenant mode). Always present.
          - `display_name`: the Garmin Connect profile's full name, or
            null if the profile is incomplete / unreachable.
          - `timezone`: an IANA timezone string (e.g. `Europe/Berlin`).
            Resolved from the user-settings endpoint when available,
            otherwise from the most-recent activity's `timeZoneId`.
            Null if neither source yields a string value.
          - `timezone_offset_min`: current UTC offset in minutes,
            derived from the most-recent activity's startTimeLocal/GMT
            delta. Null if no recent activity exists.

        Cached for 24h per user — identity / timezone change rarely.
        """
        user_id = CURRENT_USER.get() or LEGACY_USER_ID
        return {
            "user_id": user_id,
            "display_name": _resolve_display_name(),
            "timezone": _resolve_timezone(),
            "timezone_offset_min": _resolve_offset_min(),
        }


def _timezone_candidates(profile: Any) -> list[Any]:
    if not isinstance(profile, dict):
        return []
    candidates: list[Any] = []
    user_data = profile.get("userData")
    if isinstance(user_data, dict):
        candidates.extend([user_data.get("timeZone"), user_data.get("timezone")])
    candidates.extend([profile.get("timeZone"), profile.get("timezone")])
    return candidates
