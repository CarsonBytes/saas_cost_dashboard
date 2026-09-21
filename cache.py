"""Generic read-through TTL cache for Supabase queries. Eliminates redundant
reads across overlapping UI interactions and background loops.

Usage:
    from cache import ReadThroughCache

    _my_cache = ReadThroughCache(ttl=300)  # 5 minutes
    result = _my_cache.get("key", lambda: expensive_supabase_call())

The callable is only invoked on cache miss or expiry. Thread-safe for the
dashboard's single-writer / many-reader pattern (background loops write,
event-loop renders read).
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable


class ReadThroughCache:
    """TTL cache with lazy population. Keys are arbitrary strings; values are
    any JSON-serializable object. The `loader` callable passed to `get()` is
    called at most once per key per TTL window."""

    def __init__(self, ttl: float = 300, max_entries: int = 64) -> None:
        self._ttl = ttl
        self._max = max_entries
        self._store: dict[str, tuple[float, Any]] = {}
        self._lock = threading.Lock()

    def get(self, key: str, loader: Callable[[], Any]) -> Any:
        """Return cached value if fresh, otherwise call `loader()`, cache, and
        return. The loader is called OUTSIDE the lock to avoid holding it
        during network I/O."""
        now = time.monotonic()
        with self._lock:
            hit = self._store.get(key)
            if hit and now - hit[0] < self._ttl:
                return hit[1]

        # Cache miss -- load outside the lock
        value = loader()

        with self._lock:
            # Evict oldest if at capacity
            if len(self._store) >= self._max and key not in self._store:
                oldest_key = min(self._store, key=lambda k: self._store[k][0])
                del self._store[oldest_key]
            self._store[key] = (now, value)
        return value

    def invalidate(self, key: str) -> None:
        """Remove a specific key from the cache."""
        with self._lock:
            self._store.pop(key, None)

    def clear(self) -> None:
        """Drop all cached entries."""
        with self._lock:
            self._store.clear()
