"""Process-local TTL caching for Garmin API responses.

The decorator memoises tool functions on (user_id, tool_name, args, kwargs)
for the configured TTL. One TTLCache per TTL bucket so each entry expires
on its own bucket's clock.

cachetools' TTLCache is **not thread-safe** — FastMCP's HTTP transport
runs sync tools via anyio.to_thread.run_sync, so concurrent reads/writes
need a lock. We use one RLock per cache plus a registry lock for cache
creation.

Cache keys are namespaced by `user_id` (read from `registry.CURRENT_USER`)
so two different tenants never share a cached response — no
cross-tenant leak via the cache.
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Callable
from functools import wraps
from typing import Any

from cachetools import TTLCache

from garmin_mcp.registry import CURRENT_USER, LEGACY_USER_ID

# TTLs in seconds. The brief specifies these per tool category.
TTL_ACTIVITY_LIST = 60
TTL_ACTIVITY_FINAL = 24 * 60 * 60
TTL_WELLNESS = 30 * 60
TTL_TRAINING_STATUS = 60 * 60

_MAX_ENTRIES = 1024
_caches: dict[int, tuple[TTLCache[tuple[Any, ...], Any], threading.RLock]] = {}
_registry_lock = threading.Lock()


def _cache_for(ttl: int) -> tuple[TTLCache[tuple[Any, ...], Any], threading.RLock]:
    """Return (cache, lock) pair for a TTL, creating it on first use."""
    entry = _caches.get(ttl)
    if entry is not None:
        return entry
    with _registry_lock:
        # Re-check inside the lock — another thread may have created it.
        entry = _caches.get(ttl)
        if entry is None:
            entry = (TTLCache(maxsize=_MAX_ENTRIES, ttl=ttl), threading.RLock())
            _caches[ttl] = entry
        return entry


def _disabled() -> bool:
    return os.environ.get("GARMIN_MCP_NO_CACHE") == "1"


def _current_user_id() -> str:
    """Return the user_id for the active request, or `_legacy` outside HTTP."""
    return CURRENT_USER.get() or LEGACY_USER_ID


def _make_key(
    user_id: str, name: str, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> tuple[Any, ...]:
    # json.dumps(default=str) is good enough for the tool argument shapes we expect
    # (ints, strings, dates serialised to ISO). It avoids subtle bugs where
    # equal-but-distinct args (e.g. None vs missing) collide.
    return (user_id, name,
            json.dumps(args, default=str, sort_keys=True),
            json.dumps(kwargs, default=str, sort_keys=True))


def cached(ttl: int) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator: memoise the wrapped function for `ttl` seconds.

    Per-user namespaced — cache keys include the active user_id so two
    tenants never share a cached response. Bypassed entirely when
    GARMIN_MCP_NO_CACHE=1.
    """

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        cache, lock = _cache_for(ttl)
        name = fn.__qualname__

        @wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            if _disabled():
                return fn(*args, **kwargs)
            key = _make_key(_current_user_id(), name, args, kwargs)
            with lock:
                if key in cache:
                    return cache[key]
            # Compute outside the lock — Garmin RTT can be hundreds of ms,
            # we don't want to serialise unrelated tool calls behind it.
            value = fn(*args, **kwargs)
            with lock:
                cache[key] = value
            return value

        return wrapper

    return decorator


def clear_all() -> None:
    """Drop every cache bucket. Useful in tests."""
    for cache, lock in _caches.values():
        with lock:
            cache.clear()
