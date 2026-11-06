"""Search provider interface + Brave Search implementation (pluggable; paid providers can be added).

Every query is budget-checked (per audit and per month, counted in the usage ledger) and cached.
"""
from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import timedelta

import httpx

from app import cache, usage
from app.config import RuntimeSettings, env, ssl_context

BRAVE_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"


class SearchError(Exception):
    pass


class SearchBudgetExceeded(SearchError):
    pass


@dataclass
class SearchResult:
    url: str
    title: str
    snippet: str = ""
    age: str | None = None


@dataclass
class SearchResponse:
    query: str
    results: list[SearchResult] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)  # FAQ / discussion questions shown for the query
    source: str = "Brave Search"
    cached: bool = False

    def to_json(self) -> dict:
        return {"query": self.query, "results": [asdict(r) for r in self.results], "questions": self.questions,
                "source": self.source}

    @classmethod
    def from_json(cls, d: dict, cached: bool = True) -> SearchResponse:
        return cls(query=d["query"], results=[SearchResult(**r) for r in d.get("results", [])],
                   questions=d.get("questions", []), source=d.get("source", "Brave Search"), cached=cached)


class SearchProvider(ABC):
    name = "abstract"

    @abstractmethod
    async def search(self, query: str, *, count: int = 10, country: str | None = None, lang: str | None = None,
                     ttl_days: int | None = None) -> SearchResponse: ...


class BraveSearch(SearchProvider):
    name = "brave"
    _lock = asyncio.Lock()
    _last = 0.0

    def __init__(self, settings: RuntimeSettings, audit_id: int | None, api_key: str | None):
        self.s = settings
        self.audit_id = audit_id
        self.api_key = api_key
        self.used_this_audit = 0
        self.cached_hits = 0

    def _check_budget(self) -> None:
        b = self.s.budgets
        if self.used_this_audit >= b.brave_queries_per_audit:
            raise SearchBudgetExceeded(f"Search budget for this audit ({b.brave_queries_per_audit} queries) reached")
        month = usage.used("brave", month=True)
        if month >= b.brave_queries_per_month:
            raise SearchBudgetExceeded(f"Monthly search budget ({b.brave_queries_per_month} queries) reached")

    async def search(self, query: str, *, count: int = 10, country: str | None = None, lang: str | None = None,
                     ttl_days: int | None = None) -> SearchResponse:
        ttl = ttl_days or self.s.data.search_cache_days
        key = cache.cache_key("brave-v1", query, count, (country or "").upper(), (lang or "").lower())
        hit = await asyncio.to_thread(cache.get, "search", key)
        if hit:
            self.cached_hits += 1
            return SearchResponse.from_json(hit)
        await asyncio.to_thread(self._check_budget)
        params = {"q": query, "count": min(20, count), "safesearch": "moderate"}
        if country and len(country) == 2:
            params["country"] = country.upper()
        if lang:
            params["search_lang"] = lang[:2].lower()
        headers = {"Accept": "application/json", "X-Subscription-Token": self.api_key or "configured-via-proxy"}
        async with BraveSearch._lock:  # free plan: 1 request/second
            wait = 1.1 - (time.monotonic() - BraveSearch._last)
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                async with httpx.AsyncClient(verify=ssl_context(), timeout=20) as c:
                    resp = await c.get(BRAVE_ENDPOINT, params=params, headers=headers)
            except httpx.HTTPError as e:
                raise SearchError(f"Brave Search unreachable: {type(e).__name__}") from e
            finally:
                BraveSearch._last = time.monotonic()
        self.used_this_audit += 1
        await asyncio.to_thread(usage.record, "brave", "web_search", 1, 0.0, self.audit_id)
        if resp.status_code != 200:
            try:
                detail = resp.json().get("error", {}).get("detail") or resp.text[:200]
            except ValueError:
                detail = resp.text[:200]
            raise SearchError(f"Brave Search HTTP {resp.status_code}: {detail}")
        out = parse_brave(query, resp.json())
        await asyncio.to_thread(cache.put, "search", key, out.to_json(), timedelta(days=ttl))
        return out


def parse_brave(query: str, d: dict) -> SearchResponse:
    out = SearchResponse(query=query)
    for r in (d.get("web") or {}).get("results", []) or []:
        if r.get("url"):
            snippet = r.get("description") or ""
            extra = r.get("extra_snippets") or []
            out.results.append(SearchResult(url=r["url"], title=r.get("title") or "",
                                            snippet=" ".join([snippet] + extra[:2])[:600], age=r.get("age")))
    for block in ("faq", "discussions"):
        for r in (d.get(block) or {}).get("results", []) or []:
            q = r.get("question") or (r.get("data") or {}).get("question") or (r.get("title") if block == "discussions" else None)
            if q and q not in out.questions:
                out.questions.append(q)
    return out


def make_search(settings: RuntimeSettings, audit_id: int | None) -> SearchProvider | None:
    key = env().brave_api_key
    return BraveSearch(settings, audit_id, key) if key else None
