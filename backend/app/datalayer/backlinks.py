"""Live backlink discovery (a SAMPLE, never "all backlinks").

1. Search for off-site pages mentioning the domain or brand (the site itself excluded).
2. Fetch each result page (robots.txt respected, polite rate) and look for real links to the target.
3. Linked pages -> discovered backlinks (source URL, anchor, rel flags, source Authority Score).
   Pages that mention the brand but don't link -> unlinked mentions (link opportunities).
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import asdict, dataclass, field
from urllib.parse import urlsplit

from app.config import RuntimeSettings, env
from app.crawler.fetcher import Fetcher
from app.crawler.parser import parse_page
from app.crawler.rate_limit import HostRateLimiter
from app.crawler.robots import build_robots
from app.crawler.urls import SiteScope, host_of, normalize_url
from app.datalayer import authority
from app.providers.search import SearchError, SearchProvider
from app.sitemodel.types import PageData

SOCIAL_HOSTS = ("facebook.com", "instagram.com", "linkedin.com", "x.com", "twitter.com", "youtube.com",
                "pinterest.com", "tiktok.com", "reddit.com", "quora.com")
SPAM_TLDS = {"xyz", "top", "icu", "buzz", "club", "online", "site", "live", "work", "click", "link", "loan",
             "win", "bid", "rest", "fit", "cfd", "sbs", "monster", "quest"}


@dataclass
class Backlink:
    source_url: str
    source_domain: str
    target_url: str
    anchor: str
    nofollow: bool
    sponsored: bool
    ugc: bool
    authority: int | None = None
    outbound_domains: int = 0  # distinct external domains linked from the source page


@dataclass
class Mention:
    source_url: str
    source_domain: str
    title: str
    snippet: str
    authority: int | None = None


@dataclass
class Discovery:
    target_domain: str
    backlinks: list[Backlink] = field(default_factory=list)
    mentions: list[Mention] = field(default_factory=list)   # unlinked
    queries: list[str] = field(default_factory=list)
    candidates: int = 0
    pages_checked: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def referring_domains(self) -> set[str]:
        return {b.source_domain for b in self.backlinks}

    def to_json(self) -> dict:
        return {"target_domain": self.target_domain, "backlinks": [asdict(b) for b in self.backlinks],
                "mentions": [asdict(m) for m in self.mentions], "queries": self.queries,
                "candidates": self.candidates, "pages_checked": self.pages_checked, "errors": self.errors[:10]}


def registered(host: str) -> str:
    return host.lower().removeprefix("www.")


def is_target(url: str, target: str) -> bool:
    h = registered(host_of(url))
    return h == target or h.endswith("." + target)


async def discover(target_domain: str, brand: str | None, search: SearchProvider, settings: RuntimeSettings, *,
                   max_pages: int, extra_queries: list[str] | None = None, country: str | None = None) -> Discovery:
    target = registered(target_domain)
    out = Discovery(target_domain=target)
    queries = [f'"{target}" -site:{target}']
    if brand and brand.lower() not in target:
        queries.append(f'"{brand}" -site:{target}')
    queries += extra_queries or []
    seen: dict[str, tuple[str, str]] = {}
    for q in queries:
        try:
            resp = await search.search(q, count=20, country=country)
        except SearchError as e:
            out.errors.append(str(e))
            break
        out.queries.append(q)
        for r in resp.results:
            if is_target(r.url, target) or r.url in seen:
                continue
            seen[r.url] = (r.title, r.snippet)
    out.candidates = len(seen)
    cfg = settings.crawl
    brand_rx = re.compile(re.escape(brand), re.I) if brand else None
    robots_cache: dict[str, object] = {}
    sem = asyncio.Semaphore(4)
    async with Fetcher(env().user_agent, HostRateLimiter(cfg.rate_limit_per_sec), timeout=cfg.timeout_s,
                       retries=1, max_bytes=3_000_000) as f:

        async def allowed(url: str) -> str | None:
            """None when fetching is allowed, else the reason it is not."""
            origin = "{0.scheme}://{0.netloc}".format(urlsplit(url))
            if origin not in robots_cache:
                rr = await f.fetch(origin + "/robots.txt", max_bytes=256_000, retries=0)
                robots_cache[origin] = build_robots(origin + "/robots.txt", rr.status,
                                                    rr.text() if rr.status == 200 else "", rr.error)
            rb = robots_cache[origin]
            if rb.status is None:
                return f"site unreachable ({rb.error or 'no response'})"
            return None if rb.can_fetch(url, cfg.user_agent_token) else "blocked by robots.txt"

        async def check(url: str, title: str, snippet: str) -> None:
            async with sem:
                try:
                    why = await allowed(url)
                    if why:
                        out.errors.append(f"{url}: {why}")
                        return
                    res = await f.fetch(url, html_only=True, retries=0)
                except Exception as e:  # never let one page break discovery
                    out.errors.append(f"{url}: {type(e).__name__}")
                    return
                if res.status != 200 or not res.is_html or not res.body:
                    out.errors.append(f"{url}: {res.error or f'HTTP {res.status}'}")
                    return
                out.pages_checked += 1
                src_host = host_of(res.final_url or url)
                page = PageData(url=url, final_url=res.final_url or url)
                parse_page(page, res.text(), SiteScope(src_host))
                ext_domains = {registered(host_of(l.target)) for l in page.links if not l.internal}
                links = [l for l in page.links if is_target(l.target, target)]
                if links:
                    for l in links[:5]:
                        out.backlinks.append(Backlink(source_url=url, source_domain=registered(src_host),
                                                      target_url=l.target, anchor=l.anchor[:200],
                                                      nofollow=l.nofollow, sponsored=l.sponsored, ugc=l.ugc,
                                                      outbound_domains=len(ext_domains)))
                elif (brand_rx and brand_rx.search(page.text)) or target in page.text.lower():
                    out.mentions.append(Mention(source_url=url, source_domain=registered(src_host),
                                                title=title, snippet=snippet[:300]))

        await asyncio.gather(*(check(u, t, s) for u, (t, s) in list(seen.items())[:max_pages]))
    doms = list({b.source_domain for b in out.backlinks} | {m.source_domain for m in out.mentions})
    auth = await asyncio.to_thread(authority.lookup, doms) if doms else {}
    for b in out.backlinks:
        b.authority = (auth.get(b.source_domain) or {}).get("score")
    for m in out.mentions:
        m.authority = (auth.get(m.source_domain) or {}).get("score")
    return out


def suspicious_patterns(domains: dict[str, int | None], *, outbound: dict[str, int] | None = None,
                        new_vs_previous: tuple[int, int] | None = None) -> list[dict]:
    """Heuristics on whatever referrer data is available. domains: referring domain -> Authority Score."""
    flags = []
    n = len(domains)
    if n >= 5:
        low = [d for d, s in domains.items() if s is None or s < 20]
        if len(low) / n > 0.7:
            flags.append({"pattern": "High share of low-authority referrers",
                          "detail": f"{len(low)} of {n} referring domains are low authority"})
        tlds = [d.rsplit(".", 1)[-1] for d in domains]
        spam = [d for d in domains if d.rsplit(".", 1)[-1] in SPAM_TLDS]
        if len(spam) / n > 0.3:
            flags.append({"pattern": "Cluster of spam-prone TLDs",
                          "detail": f"{len(spam)} of {n} referrers use TLDs like .{', .'.join(sorted({t for t in tlds if t in SPAM_TLDS})[:5])}",
                          "examples": spam[:10]})
    farms = [d for d, k in (outbound or {}).items() if k >= 100]
    if farms:
        flags.append({"pattern": "Referrers that link to many unrelated sites",
                      "detail": f"{len(farms)} referring page(s) link to 100+ external domains", "examples": farms[:10]})
    if new_vs_previous:
        new, prev_total = new_vs_previous
        if new >= 50 and new > 2 * max(prev_total, 1):
            flags.append({"pattern": "Sudden spike in new referring domains",
                          "detail": f"{new} new referring domains since the previous release, which had {prev_total} in total"})
    return flags
