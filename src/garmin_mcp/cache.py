"""Process-local TTL caching for Garmin API responses.

The decorator memoises tool functions on (tool_name, args, kwargs) for the
configured TTL. A single shared TTLCache backs every tool — which is fine
because cache size is bounded and keys are namespaced by tool name.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from functools import wraps
from typing import Any

from cachetools import TTLCache

# TTLs in seconds. The brief specifies these per tool category.
TTL_ACTIVITY_LIST = 60
TTL_ACTIVITY_FINAL = 24 * 60 * 60
TTL_WELLNESS = 30 * 60
TTL_TRAINING_STATUS = 60 * 60

_MAX_ENTRIES = 1024
_global_cache: TTLCache[tuple[Any, ...], Any] = TTLCache(maxsize=_MAX_ENTRIES, ttl=TTL_WELLNESS)
# Per-key TTL is not natively supported by TTLCache; we use distinct caches
# per TTL bucket so each entry expires on its own bucket's clock.
_caches: dict[int, TTLCache[tuple[Any, ...], Any]] = {}


def _cache_for(ttl: int) -> TTLCache[tuple[Any, ...], Any]:
    cache = _caches.get(ttl)
    if cache is None:
        cache = TTLCache(maxsize=_MAX_ENTRIES, ttl=ttl)
        _caches[ttl] = cache
    return cache


def _disabled() -> bool:
    return os.environ.get("GARMIN_MCP_NO_CACHE") == "1"


def _make_key(name: str, args: tuple[Any, ...], kwargs: dict[str, Any]) -> tuple[Any, ...]:
    # json.dumps(default=str) is good enough for the tool argument shapes we expect
    # (ints, strings, dates serialised to ISO). It avoids subtle bugs where
    # equal-but-distinct args (e.g. None vs missing) collide.
    return (name, json.dumps(args, default=str, sort_keys=True),
            json.dumps(kwargs, default=str, sort_keys=True))


def cached(ttl: int) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator: memoise the wrapped function for `ttl` seconds.

    Bypassed entirely when GARMIN_MCP_NO_CACHE=1.
    """

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        cache = _cache_for(ttl)
        name = fn.__qualname__

        @wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            if _disabled():
                return fn(*args, **kwargs)
            key = _make_key(name, args, kwargs)
            if key in cache:
                return cache[key]
            value = fn(*args, **kwargs)
            cache[key] = value
            return value

        return wrapper

    return decorator


def clear_all() -> None:
    """Drop every cache bucket. Useful in tests."""
    for c in _caches.values():
        c.clear()
    _global_cache.clear()
