"""Shared helpers for tool modules."""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Callable
from functools import wraps
from typing import Any

from garminconnect import (
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)

from garmin_mcp.auth import AuthError

logger = logging.getLogger(__name__)


def today_iso() -> str:
    return dt.date.today().isoformat()


def coerce_date(value: str | None) -> str:
    """Validate an ISO date string or return today."""
    if not value:
        return today_iso()
    # Validates format; raises ValueError on bad input which FastMCP surfaces.
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
