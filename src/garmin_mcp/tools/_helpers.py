"""Shared helpers for tool modules."""

from __future__ import annotations

import datetime as dt
import logging
import time
from collections.abc import Callable
from functools import wraps
from typing import Any

from garminconnect import (
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)

from garmin_mcp.auth import AuthError
from garmin_mcp.registry import CURRENT_USER, LEGACY_USER_ID

logger = logging.getLogger(__name__)
audit_logger = logging.getLogger("garmin_mcp.audit")


def audited(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Emit one stderr line per tool call: `ts=... user=... tool=...`.

    Args are deliberately NOT logged — they can carry activity IDs that
    are not interesting and could grow the log. Stdio mode keeps stdout
    clean because the audit logger inherits the root handler configured
    on stderr by `_stderr_logging`.
    """
    tool_name = fn.__name__

    @wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        user_id = CURRENT_USER.get() or LEGACY_USER_ID
        audit_logger.info(
            "ts=%s user=%s tool=%s",
            time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            user_id,
            tool_name,
        )
        return fn(*args, **kwargs)

    return wrapper


def today_iso() -> str:
    return dt.date.today().isoformat()


def coerce_date(value: str | None) -> str:
    """Validate an ISO date string or return today.

    `None` defaults to today (the documented "no date supplied" path).
    An empty string is treated as bad input — it's almost certainly a caller
    bug rather than an intentional "I don't have a date", and surfacing
    `bad_argument` makes that fixable.
    """
    if value is None:
        return today_iso()
    # Raises ValueError on "" or bad format; safe_call returns bad_argument.
    dt.date.fromisoformat(value)
    return value


def safe_call(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap a tool body to translate Garmin SDK errors into structured results.

    We never let the Garmin SDK's exceptions propagate as raw MCP errors —
    callers (LLMs) get a clean dict explaining what went wrong and how to
    fix it.
    """

    @wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except AuthError as e:
            return {"error": "auth_expired", "message": str(e), "remediation": e.remediation}
        except GarminConnectAuthenticationError as e:
            return {
                "error": "auth_expired",
                "message": str(e),
                "remediation": "run `garmin-mcp auth login`",
            }
        except GarminConnectTooManyRequestsError as e:
            return {"error": "rate_limited", "message": str(e),
                    "remediation": "back off and retry in a few minutes"}
        except GarminConnectConnectionError as e:
            return {"error": "garmin_unreachable", "message": str(e)}
        except ValueError as e:
            # Bad input from the caller (e.g. malformed ISO date) — distinguish
            # from internal_error so the LLM knows it's a fixable input issue.
            return {"error": "bad_argument", "message": str(e)}
        except Exception as e:
            logger.exception("Unhandled tool error in %s", fn.__qualname__)
            return {"error": "internal_error", "message": str(e)}

    return wrapper
