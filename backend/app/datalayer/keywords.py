"""Keyword engine: ideas, search volume (with source labels) and our own difficulty estimate.

Volume, in priority order (first available wins; never invented):
  1. Google Ads Keyword Planner   (credentials required — not configured → skipped)
  2. Google Search Console        (owner access required — not configured → skipped)
  3. Google Trends relative index (0-100). Converted to an estimated volume ONLY when an anchor
     keyword with known volume exists (from 1 or 2), labelled "Estimate". Without an anchor the
     relative index is reported as-is, labelled "Google Trends (relative)".
  4. Otherwise NOT_CHECKED.

Difficulty (0-100, label "Estimate"), from the top 10 search results for the keyword:
  authority          mean Authority Score of ranking domains (outside the table counts as 10)
  content_depth      median word count of the top 5 ranking pages, 2,500+ words = 100 (if fetched)
  exact_match_titles share of the top 10 titles containing every keyword word × 100
  intent_match       share of results whose URL/title pattern matches the keyword's intent × 100
  difficulty = Σ weight × component / Σ weight over the components that could be measured.
  Default weights: authority 0.55, content_depth 0.15, exact_match_titles 0.15, intent_match 0.15.
"""
from __future__ import annotations

import asyncio
import json
import re
import statistics
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import timedelta
from urllib.parse import quote, urlsplit

import httpx

from app import cache
from app.config import RuntimeSettings, ssl_context
from app.datalayer import authority

AUTOCOMPLETE = "https://suggestqueries.google.com/complete/search"
TRENDS_EXPLORE = "https://trends.google.com/trends/api/explore"
TRENDS_MULTILINE = "https://trends.google.com/trends/api/widgetdata/multiline"
QUESTION_WORDS = ["how", "what", "why", "best", "is", "can", "where", "which", "vs"]
_W = re.compile(r"\w+", re.UNICODE)
INTENT_PATTERNS = {
    "informational": re.compile(r"\b(how|what|why|guide|tips|learn|wiki|blog|news|meaning|ideas)\b", re.I),
    "commercial": re.compile(r"\b(best|top|review|reviews|vs|compare|comparison|alternatives?)\b", re.I),
    "transactional": re.compile(r"\b(buy|price|prices|cost|shop|store|order|sale|deal|product|products|booking|hire)\b", re.I),
    "navigational": re.compile(r"\b(login|sign in|official|contact|website|home)\b", re.I),
}


class SourceUnavailable(Exception):
    pass


def kw_words(s: str) -> set[str]:
    return {w[:-1] if len(w) > 4 and w.endswith("s") else w for w in _W.findall(s.lower()) if len(w) > 1}


# ============================================================================ ideas
class Autocomplete:
    _lock = asyncio.Lock()
    _last = 0.0

    def __init__(self, hl: str = "en", gl: str | None = None, cache_days: int = 30):
        self.hl, self.gl, self.cache_days = hl, gl, cache_days
        self.requests = 0

    async def suggest(self, q: str) -> list[str]:
        key = cache.cache_key("ac-v1", q, self.hl, self.gl)
        hit = await asyncio.to_thread(cache.get, "autocomplete", key)
        if hit is not None:
            return hit
        params = {"client": "firefox", "hl": self.hl, "q": q}
        if self.gl:
            params["gl"] = self.gl.lower()
        async with Autocomplete._lock:  # be polite: ≤ 1 request/second
            wait = 1.0 - (time.monotonic() - Autocomplete._last)
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                async with httpx.AsyncClient(verify=ssl_context(), timeout=10) as c:
                    r = await c.get(AUTOCOMPLETE, params=params, headers={"User-Agent": "Mozilla/5.0"})
            except httpx.HTTPError as e:
                raise SourceUnavailable(f"Google Autocomplete unreachable ({type(e).__name__})") from e
            finally:
                Autocomplete._last = time.monotonic()
        self.requests += 1
        if r.status_code != 200:
            raise SourceUnavailable(f"Google Autocomplete HTTP {r.status_code}")
        try:
            out = [s for s in r.json()[1] if isinstance(s, str)]
        except (ValueError, IndexError, TypeError):
            raise SourceUnavailable("Unexpected Autocomplete response")
        await asyncio.to_thread(cache.put, "autocomplete", key, out, timedelta(days=self.cache_days))
        return out

    async def expand(self, seed: str, *, alphabet: bool) -> list[str]:
        queries = [seed] + [f"{w} {seed}" for w in QUESTION_WORDS]
        if alphabet:
            queries += [f"{seed} {c}" for c in "abcdefghijklmnopqrstuvwxyz"]
        found: list[str] = []
        for q in queries:
            for s in await self.suggest(q):
                if s != seed and s not in found:
                    found.append(s)
        return found


# ============================================================================ volume
@dataclass
class Volume:
    keyword: str
    value: float | None = None          # monthly searches (point) when known
    low: int | None = None              # range, when the source gives buckets
    high: int | None = None
    relative_index: float | None = None  # Google Trends 0-100 relative interest
    source: str | None = None


class VolumeProvider(ABC):
    name = "abstract"

    @abstractmethod
    async def volumes(self, keywords: list[str], country: str | None, lang: str | None) -> dict[str, Volume]: ...


class GoogleAdsVolume(VolumeProvider):
    """Keyword Planner historical metrics. Requires Ads API credentials with Basic access."""
    name = "Google Ads API"

    async def volumes(self, keywords, country, lang):
        from app.config import env
        st = env().integration_status()
        if not st.get("google_ads_keyword_planner"):
            raise SourceUnavailable("Google Ads Keyword Planner credentials not configured")
        raise SourceUnavailable("Google Ads Keyword Planner client is not implemented in this build")


class SearchConsoleVolume(VolumeProvider):
    name = "Search Console"

    async def volumes(self, keywords, country, lang):
        from app.config import env
        if not env().integration_status().get("search_console"):
            raise SourceUnavailable("Search Console access not configured")
        raise SourceUnavailable("Search Console client is not implemented in this build")


class TrendsRelative(VolumeProvider):
    """Google Trends 12-month average interest, comparable across batches via a shared anchor keyword."""
    name = "Google Trends (relative)"

    def __init__(self, cache_days: int = 30):
        self.cache_days = cache_days

    async def _batch(self, client: httpx.AsyncClient, kws: list[str], geo: str) -> dict[str, float]:
        req = {"comparisonItem": [{"keyword": k, "geo": geo, "time": "today 12-m"} for k in kws],
               "category": 0, "property": ""}
        r = await client.get(TRENDS_EXPLORE, params={"hl": "en-US", "tz": "0", "req": json.dumps(req)})
        if r.status_code != 200:
            raise SourceUnavailable(f"Google Trends HTTP {r.status_code} (often rate-limited from server IPs)")
        widgets = json.loads(r.text[r.text.find("{"):])["widgets"]
        w = next(x for x in widgets if x.get("id") == "TIMESERIES")
        r2 = await client.get(TRENDS_MULTILINE, params={"hl": "en-US", "tz": "0", "req": json.dumps(w["request"]),
                                                        "token": w["token"]})
        if r2.status_code != 200:
            raise SourceUnavailable(f"Google Trends HTTP {r2.status_code}")
        data = json.loads(r2.text[r2.text.find("{"):])["default"]["timelineData"]
        out = {}
        for i, k in enumerate(kws):
            vals = [pt["value"][i] for pt in data if pt.get("value")]
            out[k] = statistics.mean(vals) if vals else 0.0
        return out

    async def volumes(self, keywords, country, lang):
        geo = (country or "").upper()[:2]
        key = cache.cache_key("trends-v1", sorted(keywords), geo)
        hit = await asyncio.to_thread(cache.get, "trends", key)
        if hit is not None:
            return {k: Volume(k, relative_index=v, source=self.name) for k, v in hit.items()}
        async with httpx.AsyncClient(verify=ssl_context(), timeout=20, headers={"User-Agent": "Mozilla/5.0"}) as c:
            await c.get("https://trends.google.com/trends/?geo=" + geo)  # session cookie
            anchor = keywords[0]
            rel: dict[str, float] = {}
            base = None
            for i in range(0, len(keywords), 4):
                group = [anchor] + [k for k in keywords[i:i + 4] if k != anchor]
                vals = await self._batch(c, group, geo)
                if base is None:
                    base = vals[anchor] or 1.0
                scale = base / (vals[anchor] or 1.0)
                for k in group:
                    rel[k] = round(vals[k] * scale, 1)
                await asyncio.sleep(1.5)
        await asyncio.to_thread(cache.put, "trends", key, rel, timedelta(days=self.cache_days))
        return {k: Volume(k, relative_index=v, source=self.name) for k, v in rel.items()}


async def volume_chain(keywords: list[str], country: str | None, lang: str | None,
                       providers: list[VolumeProvider]) -> tuple[dict[str, Volume], list[str]]:
    """First provider that works wins. Returns (volumes, notes about unavailable sources)."""
    notes = []
    for p in providers:
        try:
            v = await p.volumes(keywords, country, lang)
            return v, notes
        except SourceUnavailable as e:
            notes.append(f"{p.name}: {e}")
        except (httpx.HTTPError, ValueError, KeyError, StopIteration) as e:
            notes.append(f"{p.name}: failed ({type(e).__name__})")
    return {}, notes


# ============================================================================ difficulty
@dataclass
class Difficulty:
    keyword: str
    score: int | None
    components: dict[str, float] = field(default_factory=dict)
    top_domains: list[dict] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)
    note: str | None = None
    source: str = "Estimate (Brave Search top 10 + Common Crawl authority)"


def intent_of_result(url: str, title: str) -> str | None:
    text = f"{urlsplit(url).path.replace('-', ' ').replace('/', ' ')} {title}"
    for intent in ("transactional", "commercial", "informational", "navigational"):
        if INTENT_PATTERNS[intent].search(text):
            return intent
    return None


def compute_difficulty(keyword: str, intent: str | None, results: list, auth: dict[str, dict | None],
                       depths: list[int], weights: dict[str, float]) -> Difficulty:
    top = results[:10]
    if not top:
        return Difficulty(keyword, None, note="No search results")
    doms = [(urlsplit(r.url).hostname or "").removeprefix("www.") for r in top]
    scores = [((auth.get(d) or {}).get("score") or 10) for d in doms]
    comps: dict[str, float] = {"authority": statistics.mean(scores)}
    if depths:
        comps["content_depth"] = min(100.0, statistics.median(depths) / 25)
    need = kw_words(keyword)
    if need:
        comps["exact_match_titles"] = 100 * sum(1 for r in top if need <= kw_words(r.title)) / len(top)
    if intent:
        comps["intent_match"] = 100 * sum(1 for r in top if intent_of_result(r.url, r.title) == intent) / len(top)
    wsum = sum(weights.get(k, 0) for k in comps)
    value = round(sum(weights.get(k, 0) * v for k, v in comps.items()) / wsum) if wsum else None
    return Difficulty(keyword, value, {k: round(v, 1) for k, v in comps.items()},
                      [{"domain": d, "authority": (auth.get(d) or {}).get("score")} for d in doms])


async def fetch_word_counts(urls: list[str], timeout: float = 15.0) -> list[int]:
    """Word counts of ranking pages (best-effort; failures are skipped)."""
    from selectolax.lexbor import LexborHTMLParser
    out = []

    async def one(u: str) -> None:
        try:
            async with httpx.AsyncClient(verify=ssl_context(), timeout=timeout, follow_redirects=True,
                                         headers={"User-Agent": "Mozilla/5.0 (compatible; SEOInspector)"}) as c:
                r = await c.get(u)
            if r.status_code == 200 and "html" in r.headers.get("content-type", ""):
                t = LexborHTMLParser(r.text[:3_000_000])
                t.strip_tags(["script", "style", "noscript", "nav", "header", "footer"])
                if t.body is not None:
                    out.append(len(t.body.text(separator=" ").split()))
        except Exception:
            return
    await asyncio.gather(*(one(u) for u in urls))
    return out


async def difficulty(keyword: str, intent: str | None, search, settings: RuntimeSettings, *,
                     country: str | None = None, lang: str | None = None, fetch_pages: bool = True) -> Difficulty:
    resp = await search.search(keyword, count=10, country=country, lang=lang,
                               ttl_days=settings.data.keyword_cache_days)
    doms = list({(urlsplit(r.url).hostname or "").removeprefix("www.") for r in resp.results[:10]})
    auth = await asyncio.to_thread(authority.lookup, doms)
    depths = await fetch_word_counts([r.url for r in resp.results[:5]]) if fetch_pages else []
    d = compute_difficulty(keyword, intent, resp.results, auth, depths, settings.data.difficulty_weights)
    d.questions = resp.questions[:8]
    rel = next((a["release"] for a in auth.values() if a), None)
    if rel:
        d.source = f"Estimate (Brave Search top 10 + {authority.source_label(rel)})"
    return d


def autocomplete_url(q: str) -> str:
    return f"{AUTOCOMPLETE}?client=firefox&q={quote(q)}"
