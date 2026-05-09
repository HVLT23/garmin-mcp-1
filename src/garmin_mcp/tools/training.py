"""Training-status MCP tools."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from garminconnect import Garmin
from mcp.server.fastmcp import FastMCP

from garmin_mcp.cache import TTL_TRAINING_STATUS, cached
from garmin_mcp.tools._helpers import coerce_date, safe_call

ClientFactory = Callable[[], Garmin]


def _extract_training_load(status: dict[str, Any]) -> dict[str, Any]:
    """Pull ATL/CTL/TSB-style figures from get_training_status payload.

    Garmin's payload nests load fields under several keys depending on device.
    We surface the common locations and keep raw nested dicts so the LLM can
    follow up with `get_training_status` for anything missing.
    """
    out: dict[str, Any] = {}
    for key in (
        "mostRecentTrainingLoadBalance",
        "mostRecentTrainingStatus",
        "mostRecentVO2Max",
        "loadTunnel",
    ):
        if isinstance(status, dict) and key in status:
            out[key] = status[key]
    return out


def register(mcp: FastMCP, client_factory: ClientFactory) -> None:
    @mcp.tool()
    @safe_call
    @cached(ttl=TTL_TRAINING_STATUS)
    def get_training_status(date: str | None = None) -> dict[str, Any]:
        """Garmin training status (productive, maintaining, detraining, etc.).

        Defaults to today.
        """
        return client_factory().get_training_status(coerce_date(date))

    @mcp.tool()
    @safe_call
    @cached(ttl=TTL_TRAINING_STATUS)
    def get_training_readiness(date: str | None = None) -> dict[str, Any]:
        """Morning training readiness score and the inputs that drove it.

        Defaults to today.
        """
        return client_factory().get_training_readiness(coerce_date(date))

    @mcp.tool()
    @safe_call
    @cached(ttl=TTL_TRAINING_STATUS)
    def get_vo2_max(date: str | None = None) -> dict[str, Any]:
        """Latest VO2 max estimate(s) (running and cycling, if available).

        Defaults to today.
        """
        return client_factory().get_max_metrics(coerce_date(date))

    @mcp.tool()
    @safe_call
    @cached(ttl=TTL_TRAINING_STATUS)
    def get_training_load(date: str | None = None) -> dict[str, Any]:
        """Training load context (ATL/CTL/TSB-equivalent fields) for a date.

        Distilled from the Garmin training-status payload.
        """
        status = client_factory().get_training_status(coerce_date(date))
        return _extract_training_load(status) if isinstance(status, dict) else {}

    @mcp.tool()
    @safe_call
    @cached(ttl=TTL_TRAINING_STATUS)
    def get_race_predictor() -> dict[str, Any]:
        """Garmin's race-time predictions across standard distances."""
        return client_factory().get_race_predictions() or {}
