"""Bearer-token registry for multi-tenant auth.

Maps `sha256(bearer)` → `user_id`, loaded from a JSON file (default
`/data/tokens/registry.json` — kept inside the per-user tokens root so
it lives on the same persistent volume the tokens themselves are
mounted from). The file is hot-reloadable: the registry caches parsed
entries keyed by mtime+size and reloads transparently when the file
changes on disk, so adding a new user via `garmin-mcp admin provision`
doesn't require a server restart.

Format:
    {
      "users": [
        {"user_id": "alice", "bearer_sha256": "<hex digest>"},
        {"user_id": "bob",   "bearer_sha256": "<hex digest>"}
      ]
    }

Empty / missing file → empty registry → caller falls back to the legacy
single-bearer path in the middleware.
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import logging
import os
import threading
from pathlib import Path

logger = logging.getLogger(__name__)


DEFAULT_REGISTRY_PATH = Path("/data/tokens/registry.json")
LEGACY_USER_ID = "_legacy"


# ContextVar consumed by the per-user client factory; set by the bearer
# middleware on every authenticated HTTP request. Sync tools invoked via
# `anyio.to_thread.run_sync` inherit the current contextvars from the
# request task (Python 3.11+), which is how this propagates without
# threading the user_id through every tool signature.
CURRENT_USER: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "garmin_mcp_current_user", default=None
)


class RegistryError(RuntimeError):
    """Raised when the registry file is present but malformed."""


class Registry:
    """Bearer-sha256 → user_id lookup with mtime-based hot reload.

    Thread-safe: a single lock guards the cached snapshot. The snapshot
    is replaced atomically; lookups copy a reference and read outside
    the lock.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._mapping: dict[str, str] = {}
        self._stat: tuple[int, int] | None = None
        self._loaded_once = False

    @property
    def path(self) -> Path:
        return self._path

    def _current_stat(self) -> tuple[int, int] | None:
        """Return (mtime_ns, size) of the registry file, or None if absent.

        Uses nanosecond mtime so a write that lands within the same second
        as a previous load is still detected — coarse second-resolution
        mtime would let a quickly-following provision invocation slip past
        the hot-reload check on filesystems that round to the second.
        """
        try:
            st = self._path.stat()
        except FileNotFoundError:
            return None
        except OSError as e:
            logger.warning("registry stat() failed for %s: %s", self._path, e)
            return None
        return (st.st_mtime_ns, st.st_size)

    def _reload_locked(self, stat: tuple[int, int] | None) -> None:
        """Read the file and replace the cached mapping. Caller holds the lock."""
        if stat is None:
            self._mapping = {}
            self._stat = None
            self._loaded_once = True
            return
        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            self._mapping = {}
            self._stat = None
            self._loaded_once = True
            return
        try:
            doc = json.loads(raw)
        except json.JSONDecodeError as e:
            raise RegistryError(f"registry {self._path} is not valid JSON: {e}") from e
        mapping = _validate(doc, source=str(self._path))
        self._mapping = mapping
        self._stat = stat
        self._loaded_once = True

    def _ensure_fresh(self) -> dict[str, str]:
        """Reload from disk if the file has changed; return current mapping."""
        stat = self._current_stat()
        with self._lock:
            if not self._loaded_once or stat != self._stat:
                self._reload_locked(stat)
            return self._mapping

    def lookup(self, bearer: str) -> str | None:
        """Return the user_id for a plaintext bearer, or None if not registered."""
        if not bearer:
            return None
        digest = hashlib.sha256(bearer.encode("utf-8")).hexdigest()
        mapping = self._ensure_fresh()
        return mapping.get(digest)

    def is_empty(self) -> bool:
        """True iff the registry has no entries (file missing or `users: []`)."""
        return not self._ensure_fresh()

    def user_ids(self) -> list[str]:
        """Sorted list of registered user_ids (for `admin list`)."""
        return sorted(self._ensure_fresh().values())

    def entries(self) -> list[tuple[str, str]]:
        """Snapshot of (user_id, bearer_sha256) tuples."""
        mapping = self._ensure_fresh()
        return sorted(((uid, digest) for digest, uid in mapping.items()),
                      key=lambda x: x[0])


def _validate(doc: object, *, source: str) -> dict[str, str]:
    """Validate a parsed JSON document and return the sha256→user_id mapping."""
    if not isinstance(doc, dict):
        raise RegistryError(f"registry {source}: top-level must be a JSON object")
    users = doc.get("users", [])
    if not isinstance(users, list):
        raise RegistryError(f"registry {source}: 'users' must be a list")
    mapping: dict[str, str] = {}
    seen_user_ids: set[str] = set()
    for i, entry in enumerate(users):
        if not isinstance(entry, dict):
            raise RegistryError(
                f"registry {source}: users[{i}] must be an object"
            )
        user_id = entry.get("user_id")
        digest = entry.get("bearer_sha256")
        if not isinstance(user_id, str) or not user_id:
            raise RegistryError(
                f"registry {source}: users[{i}].user_id must be a non-empty string"
            )
        if not _is_safe_user_id(user_id):
            # Defence-in-depth: a hand-edited registry containing
            # `user_id = "../etc"` would otherwise resolve to
            # `<root>/../etc` in the per-user client cache. The admin CLI
            # also rejects these patterns at provision/revoke time, but
            # validating here means a malformed file alone can never
            # produce a path-traversal lookup, even if someone bypasses
            # the CLI to edit the registry by hand.
            raise RegistryError(
                f"registry {source}: users[{i}].user_id={user_id!r} is "
                "unsafe (slashes, backslashes, leading dot, or '.'/'..')"
            )
        if not isinstance(digest, str) or not _looks_like_sha256(digest):
            raise RegistryError(
                f"registry {source}: users[{i}].bearer_sha256 must be a 64-char hex digest"
            )
        digest = digest.lower()
        if user_id in seen_user_ids:
            raise RegistryError(
                f"registry {source}: duplicate user_id {user_id!r}"
            )
        if digest in mapping:
            raise RegistryError(
                f"registry {source}: duplicate bearer_sha256 (collision between users)"
            )
        seen_user_ids.add(user_id)
        mapping[digest] = user_id
    return mapping


def _is_safe_user_id(s: str) -> bool:
    """True iff `s` is safe to interpolate into `<root>/<s>/`.

    Mirrors the admin-CLI guard so a hand-edited or future-bypassed
    registry can't introduce path traversal at lookup time.
    """
    return (
        bool(s)
        and "/" not in s
        and "\\" not in s
        and not s.startswith(".")
        and s not in ("..", ".")
    )


def _looks_like_sha256(s: str) -> bool:
    if len(s) != 64:
        return False
    try:
        int(s, 16)
    except ValueError:
        return False
    return True


def resolve_path(explicit: Path | None = None) -> Path:
    """Pick the registry file path: explicit > env > default."""
    if explicit is not None:
        return explicit
    env = os.environ.get("GARMIN_REGISTRY_PATH")
    if env:
        return Path(env)
    return DEFAULT_REGISTRY_PATH


def load_registry(path: Path | None = None) -> Registry:
    """Construct a Registry; first read happens lazily on first lookup."""
    return Registry(resolve_path(path))


def write_registry_atomic(path: Path, entries: list[dict[str, str]]) -> None:
    """Atomically replace the registry file with the given entries.

    Writes to a sibling temp file, fsyncs, then renames over the target.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"users": entries}
    tmp = path.with_suffix(path.suffix + ".tmp")
    data = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
