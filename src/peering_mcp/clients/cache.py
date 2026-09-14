"""A disk-backed cache of successful upstream responses.

Load-bearing rather than an optimisation. PeeringDB allows one request per
second, and an agent process is short-lived: an in-memory cache would be cold
on every launch, which is exactly the moment the limit hurts. So the cache has
to outlive the process, which is what `diskcache` buys.

**Only successful responses are cached. A failure is never remembered.** A 404
is the answer most likely to become wrong — a network that registers today
would look absent for a day — and a 500 or a throttle is not an answer at all.
Remembering either would turn a passing problem into a lasting one.

**One TTL for everything, and 24 hours is conservative.** The design left the
TTL open, wondering whether presence records wanted a shorter one and
facilities a longer one. Measured against 61,855 real facility-presence records
on 2026-09-14, the question turned out to be backwards: presence records are
the stalest objects in PeeringDB, with a median last edit **4.6 years** ago,
and **0.15% of them changed in the last 24 hours**. Exchanges are the liveliest
and still only 0.08%. Per-endpoint TTLs would be machinery in exchange for
nothing measurable.

Worth stating plainly, because it is easy to worry about the wrong thing: **the
staleness in this system is PeeringDB, not the cache.** A record served from
here is, on median, years old at source. A day of caching adds a rounding
error to that, and it is why every answer carries `record_updated` rather than
leaving the caller to assume the data is current.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from types import TracebackType
from typing import Any, Protocol, Self
from urllib.parse import urlencode

import diskcache

logger = logging.getLogger(__name__)


def cache_key(base_url: str, path: str, params: dict[str, str | int] | None) -> str:
    """A stable key for one upstream request.

    Sorted, so that two callers passing the same parameters in a different
    order share an entry. The API key is deliberately not part of the key: it
    changes the rate limit, never the response.
    """
    query = urlencode(sorted((k, str(v)) for k, v in (params or {}).items()))
    return f"GET {base_url.rstrip('/')}{path}?{query}"


class Cache(Protocol):
    """What the HTTP core needs from a cache. Two implementations, one real."""

    def get(self, key: str) -> dict[str, Any] | None: ...

    def set(self, key: str, payload: dict[str, Any]) -> None: ...

    def close(self) -> None: ...


class NullCache:
    """Caches nothing. Used when caching is switched off, and when it breaks.

    A cache that cannot be opened must not stop a read-only lookup server from
    answering questions, so a failure here degrades to this rather than
    raising. Same judgement as a malformed environment variable falling back to
    its default.
    """

    def get(self, key: str) -> dict[str, Any] | None:
        return None

    def set(self, key: str, payload: dict[str, Any]) -> None:
        return None

    def close(self) -> None:
        return None


class DiskCache:
    """Responses on disk, surviving a process restart."""

    def __init__(self, directory: Path, ttl_seconds: int) -> None:
        self._ttl = ttl_seconds
        self._cache = diskcache.Cache(str(directory))

    def get(self, key: str) -> dict[str, Any] | None:
        value = self._cache.get(key)
        return value if isinstance(value, dict) else None

    def set(self, key: str, payload: dict[str, Any]) -> None:
        self._cache.set(key, payload, expire=self._ttl)

    def close(self) -> None:
        self._cache.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


def default_cache_dir() -> Path:
    """Where responses live when nothing says otherwise.

    XDG on Linux, and a sane fallback everywhere else. No dependency on a
    platform-directories package for one path.
    """
    base = os.environ.get("XDG_CACHE_HOME")
    root = Path(base) if base else Path.home() / ".cache"
    return root / "peering-mcp"


def build_cache(*, enabled: bool, ttl_seconds: int, directory: Path | None) -> Cache:
    """Open the cache, or return one that does nothing.

    A TTL of zero means the same as switched off: an entry that expires
    immediately is a write nobody reads.
    """
    if not enabled or ttl_seconds <= 0:
        return NullCache()

    target = directory or default_cache_dir()
    try:
        target.mkdir(parents=True, exist_ok=True)
        return DiskCache(target, ttl_seconds)
    except Exception as exc:
        # Any failure here degrades to no cache; none of them is worth
        # refusing to answer a read-only question over.
        logger.warning("cache unavailable at %s, continuing without it: %s", target, exc)
        return NullCache()
