"""Per-site politeness: at most N requests per second per site key."""
from __future__ import annotations

import asyncio

from app.crawler.urls import host_of, site_key


class HostRateLimiter:
    def __init__(self, rate_per_sec: float):
        self.default_interval = 1.0 / rate_per_sec
        self._intervals: dict[str, float] = {}
        self._next: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def set_min_interval(self, host: str, seconds: float) -> None:
        """Honor robots.txt Crawl-delay (capped by the caller)."""
        key = site_key(host)
        self._intervals[key] = max(self.default_interval, seconds)

    async def wait(self, url_or_host: str) -> None:
        host = host_of(url_or_host) if "://" in url_or_host else url_or_host
        key = site_key(host)
        lock = self._locks.setdefault(key, asyncio.Lock())
        loop = asyncio.get_running_loop()
        async with lock:
            now = loop.time()
            at = self._next.get(key, 0.0)
            if at > now:
                await asyncio.sleep(at - now)
                now = loop.time()
            self._next[key] = now + self._intervals.get(key, self.default_interval)
