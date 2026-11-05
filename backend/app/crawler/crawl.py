"""Crawl a site once and build the shared Site Model.

Stages: start page -> robots.txt -> llms.txt -> sitemaps -> BFS crawl (raw HTTP, JS render
only when needed) -> status checks for uncrawled links/sitemap URLs -> image probes ->
template render samples (image display sizes) -> URL-variant probes.
"""
from __future__ import annotations

import asyncio
import itertools
import secrets
import logging
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import fields
from urllib.parse import urlsplit, urlunsplit

from app.config import CrawlSettings
from app.crawler.fetcher import Fetcher, FetchResult
from app.crawler.imageinfo import probe_image
from app.crawler.parser import parse_page
from app.crawler.rate_limit import HostRateLimiter
from app.crawler.renderer import Renderer
from app.crawler.robots import AI_BOTS, RobotsInfo, build_robots
from app.crawler.sitemap import collect_sitemaps
from app.crawler.urls import (
    SiteScope, ensure_scheme, host_of, looks_like_page, normalize_url, origin_of, toggle_www,
)
from app.sitemodel.builder import build_model
from app.sitemodel.types import ImageAsset, PageData, SiteFacts, SiteModel, UrlStatus

log = logging.getLogger(__name__)

ProgressCb = Callable[[dict], Awaitable[None]]
CancelCb = Callable[[], Awaitable[bool]]

SITEMAP_PRIORITY = 1000
_PARSE_FIELDS = {f.name for f in fields(PageData)} - {
    "url", "final_url", "status", "redirect_chain", "redirect_loop", "response_ms", "content_type",
    "is_html", "error", "depth", "discovered_via", "blocked_by_robots"}


class CrawlError(Exception):
    """The site cannot be audited at all (unreachable, blocked, not HTML)."""


class CrawlCancelled(Exception):
    pass


def _raw_signals(p: PageData) -> dict:
    return {"title": p.title, "meta_description": p.meta_description, "canonical": p.canonical,
            "meta_robots": p.meta_robots, "word_count": p.word_count,
            "internal_links": sum(1 for l in p.links if l.internal),
            "h1": [t for lvl, t in p.headings if lvl == 1][:3]}


class Crawler:
    def __init__(self, input_url: str, cfg: CrawlSettings, user_agent: str, *,
                 progress: ProgressCb | None = None, cancelled: CancelCb | None = None,
                 verify_tls: bool = True):
        self.input_url = input_url
        self.cfg = cfg
        self.user_agent = user_agent
        self.ua_token = cfg.user_agent_token
        self.progress = progress
        self.cancelled = cancelled
        self.verify_tls = verify_tls
        self.limiter = HostRateLimiter(cfg.rate_limit_per_sec)
        self.pages: dict[str, PageData] = {}
        self.url_status: dict[str, UrlStatus] = {}
        self.seen: set[str] = set()
        self.fetched = 0
        self.rendered_pages = 0
        self.js_site = False
        self._seq = itertools.count()
        self._last_progress = 0.0
        self.robots: RobotsInfo | None = None
        self.scope: SiteScope | None = None
        self.renderer: Renderer | None = None
        self.fetcher: Fetcher | None = None

    # ------------------------------------------------------------------ helpers
    async def _report(self, stage: str, force: bool = False, **extra) -> None:
        now = time.monotonic()
        if not self.progress or (not force and now - self._last_progress < 1.0):
            return
        self._last_progress = now
        await self.progress({"stage": stage, "fetched": self.fetched, "limit": self.cfg.max_pages,
                             "rendered": self.rendered_pages, **extra})

    async def _check_cancel(self) -> None:
        if self.cancelled and await self.cancelled():
            raise CrawlCancelled()

    def _allowed(self, url: str) -> bool:
        assert self.scope and self.robots
        return not self.scope.is_internal(url) or self.robots.can_fetch(url, self.ua_token)

    def _page_from_fetch(self, url: str, res: FetchResult, depth: int | None, via: str) -> PageData:
        status = res.chain[0].status if res.chain else res.status
        return PageData(url=url, final_url=res.final_url, status=status,
                        redirect_chain=[{"url": h.url, "status": h.status, "location": h.location}
                                        for h in res.chain],
                        redirect_loop=res.redirect_loop or res.too_many_redirects,
                        response_ms=res.elapsed_ms, content_type=res.content_type,
                        is_html=res.is_html, error=res.error, depth=depth, discovered_via=via,
                        blocked_by_robots=res.blocked_redirect)

    # ------------------------------------------------------------------ main
    async def run(self) -> SiteModel:
        start = normalize_url(ensure_scheme(self.input_url))
        if not start:
            raise CrawlError(f"Not a valid http(s) URL: {self.input_url!r}")
        t0 = time.monotonic()
        async with Fetcher(self.user_agent, self.limiter, timeout=self.cfg.timeout_s,
                           retries=self.cfg.retries, max_bytes=self.cfg.max_html_bytes,
                           verify=self.verify_tls) as f:
            self.fetcher = f
            self.renderer = Renderer(self.user_agent, self.limiter, self.cfg.render_timeout_s)
            try:
                return await self._run(f, start, t0)
            finally:
                await self.renderer.close()

    async def _run(self, f: Fetcher, start: str, t0: float) -> SiteModel:
        await self._report("crawl", force=True, message="Fetching start page")
        https_error = None
        res = await f.fetch(start, html_only=True)
        if res.status is None and start.startswith("https://"):
            https_error = res.error
            alt = "http://" + start[len("https://"):]
            res2 = await f.fetch(alt, html_only=True)
            if res2.status is not None:
                start, res = alt, res2
        if res.status is None:
            raise CrawlError(f"Could not reach {start}: {res.error or 'no response'}")
        final = normalize_url(res.final_url) or start
        if res.status >= 400:
            raise CrawlError(f"The start URL returned HTTP {res.status} to the crawler "
                             f"(the site may block bots). Nothing to audit.")
        if not res.is_html:
            raise CrawlError(f"The start URL is not an HTML page (content-type {res.content_type!r}).")

        host = host_of(final)
        self.scope = SiteScope(host, self.cfg.include_subdomains)
        origin = origin_of(final)
        facts = SiteFacts(input_url=self.input_url, start_url=final, origin=origin, host=host)

        # robots.txt
        rr = await f.fetch(origin + "/robots.txt", max_bytes=512_000)
        rtext = rr.text() if rr.status == 200 else ""
        robots_status, robots_note = rr.status, None
        if rr.status == 200 and (rr.is_html or rtext.lstrip()[:15].lower().startswith(("<!doctype", "<html"))):
            # served an HTML page (SPA fallback / soft 404): there is no real robots.txt
            robots_status, robots_note, rtext = 404, "robots.txt URL returns an HTML page (HTTP 200)", ""
        self.robots = build_robots(origin + "/robots.txt", robots_status, rtext, rr.error)
        f.allow = self._allowed
        delay = self.robots.crawl_delay(self.ua_token)
        if delay:
            self.limiter.set_min_interval(host, min(delay, 10.0))
        facts.robots = {"url": self.robots.url, "status": self.robots.status, "error": self.robots.error,
                        "http_status": rr.status, "note": robots_note,
                        "size": len(self.robots.text), "sitemaps": self.robots.sitemaps,
                        "crawl_delay": delay, "text": self.robots.text[:20_000],
                        "groups": [{"agents": g.agents, "rules": g.rules[:200]} for g in self.robots.groups][:50]}
        facts.ai_bots = self.robots.ai_bot_rules(origin, AI_BOTS)

        # llms.txt
        lr = await f.fetch(origin + "/llms.txt", max_bytes=256_000)
        ltext = lr.text() if lr.status == 200 else ""
        facts.llms_txt = {"status": lr.status, "final_url": lr.final_url,
                          "present": lr.status == 200 and bool(ltext.strip()) and "<html" not in ltext[:500].lower(),
                          "size": len(ltext), "excerpt": ltext[:1000]}

        # sitemaps
        await self._report("crawl", force=True, message="Reading sitemaps")
        sm = await collect_sitemaps(f, origin, self.robots.sitemaps, self.cfg.max_sitemap_urls)
        facts.sitemaps = [x.as_dict() for x in sm.files]
        facts.sitemap_found = sm.found
        facts.sitemap_url_count = len(sm.urls)
        facts.sitemap_truncated = sm.truncated
        facts.sitemap_soft_404_probes = sm.soft_404_probes

        # BFS crawl
        queue: asyncio.PriorityQueue = asyncio.PriorityQueue()
        self.seen.add(start)
        self.seen.add(final)
        self.fetched = 1
        await self._ingest(start, res, 0, "start", queue)
        if self.robots.unreachable:
            log.info("robots.txt unreachable (%s): crawling the start page only", self.robots.status)
        else:
            await self._crawl_queue(queue, sm.urls)

        # status checks for things we did not crawl
        await self._report("links", force=True, message="Checking links and sitemap URLs")
        await self._check_uncrawled(sm.urls)
        await self._report("images", force=True, message="Checking images")
        images = await self._probe_images()
        await self._render_samples()
        await self._report("variants", force=True, message="Checking URL variants")
        facts.https, facts.variants = await self._probe_variants(final, res, https_error)
        facts.js_site = self.js_site
        facts.crawl = {
            "pages_fetched": self.fetched, "limit": self.cfg.max_pages,
            "limit_reached": self.fetched >= self.cfg.max_pages,
            "rendered_pages": self.rendered_pages, "requests": f.request_count,
            "duration_s": round(time.monotonic() - t0, 1),
            "renderer_error": self.renderer.unavailable if self.renderer else None,
            "robots_unreachable": self.robots.unreachable,
        }
        model = SiteModel(facts=facts, pages=self.pages, images=images, url_status=self.url_status,
                          sitemap_urls=sm.urls, robots=self.robots)
        return build_model(model)

    # ------------------------------------------------------------------ BFS
    async def _crawl_queue(self, queue: asyncio.PriorityQueue, sitemap_urls: dict[str, str | None]) -> None:
        sitemap_seeded = False
        active = 0
        lock = asyncio.Lock()

        async def worker() -> None:
            nonlocal active
            while True:
                try:
                    prio, _, url, depth, via = queue.get_nowait()
                except asyncio.QueueEmpty:
                    if active == 0:
                        return
                    await asyncio.sleep(0.05)
                    continue
                async with lock:
                    if self.fetched >= self.cfg.max_pages:
                        queue.task_done()
                        continue
                    self.fetched += 1
                    active += 1
                try:
                    await self._check_cancel()
                    r = await self.fetcher.fetch(url, html_only=True)
                    await self._ingest(url, r, depth, via, queue, prio)
                    await self._report("crawl", queued=queue.qsize(), message=f"Crawling {url}")
                finally:
                    active -= 1
                    queue.task_done()

        while True:
            workers = [asyncio.create_task(worker()) for _ in range(self.cfg.concurrency)]
            try:
                await asyncio.gather(*workers)
            except BaseException:
                for w in workers:
                    w.cancel()
                raise
            if sitemap_seeded or self.fetched >= self.cfg.max_pages:
                break
            sitemap_seeded = True
            added = 0
            for u in sitemap_urls:
                if u in self.seen or not self.scope.is_internal(u) or not looks_like_page(u):
                    continue
                if not self.robots.can_fetch(u, self.ua_token):
                    continue
                self.seen.add(u)
                queue.put_nowait((SITEMAP_PRIORITY, next(self._seq), u, None, "sitemap"))
                added += 1
                if added >= self.cfg.max_pages - self.fetched:
                    break
            if not added:
                break

    async def _ingest(self, url: str, res: FetchResult, depth: int | None, via: str,
                      queue: asyncio.PriorityQueue, prio: int = 0) -> None:
        page = self._page_from_fetch(url, res, depth, via)
        self.pages[url] = page
        target = page
        if res.chain and res.status is not None and not page.redirect_loop and not res.blocked_redirect:
            final = normalize_url(res.final_url)
            if final and self.scope.is_internal(final) and final not in self.pages:
                self.seen.add(final)
                target = self._page_from_fetch(final, res, depth, via)
                target.redirect_chain = []
                target.status = res.status
                target.redirect_loop = False
                self.pages[final] = target
            else:
                return
        elif res.chain:
            return
        if not (target.is_html and target.status == 200 and res.body):
            return
        html = res.text()
        signals = parse_page(target, html, self.scope)
        is_home = via == "start"
        want_render = self._should_render(signals.looks_like_shell, is_home)
        if want_render:
            rr = await self.renderer.render(target.url, load_images=is_home)
            if rr.html and not rr.error:
                self.rendered_pages += 1
                raw = _raw_signals(target)
                fresh = PageData(url=target.url, final_url=target.final_url)
                parse_page(fresh, rr.html, self.scope)
                for name in _PARSE_FIELDS:
                    setattr(target, name, getattr(fresh, name))
                target.rendered = True
                target.raw_signals = raw
                rendered_links = sum(1 for l in target.links if l.internal)
                if (target.word_count > raw["word_count"] * 1.3 + 50
                        or rendered_links > raw["internal_links"] * 1.5 + 5):
                    target.js_dependent = True
                    if is_home:
                        self.js_site = True
                if rr.images:
                    self._apply_image_rects(target, rr.images, 900)
        for link in target.links:
            if not link.internal:
                continue
            t = link.target
            if t in self.seen or not looks_like_page(t):
                continue
            self.seen.add(t)
            if not self.robots.can_fetch(t, self.ua_token):
                self.url_status[t] = UrlStatus(url=t, status=None, blocked_by_robots=True)
                continue
            child_depth = depth + 1 if depth is not None else None
            queue.put_nowait((prio + 1, next(self._seq), t, child_depth, "link"))

    def _should_render(self, looks_like_shell: bool, is_home: bool) -> bool:
        mode = self.cfg.render_mode
        if mode == "never" or self.renderer is None or self.renderer.unavailable:
            return False
        if self.rendered_pages >= self.cfg.max_rendered_pages:
            return False
        if mode == "always" or is_home:
            return True
        return self.js_site or looks_like_shell

    @staticmethod
    def _apply_image_rects(page: PageData, rects: list[dict], viewport_h: int) -> None:
        by_src: dict[str, dict] = {}
        for r in rects:
            n = normalize_url(r.get("src") or "")
            if n:
                by_src.setdefault(n, r)
        for i, img in enumerate(page.images):
            r = by_src.get(img.src)
            if r is None and len(rects) == len(page.images):
                r = rects[i]
            if r is None:
                continue
            img.rendered_w, img.rendered_h = r.get("w") or None, r.get("h") or None
            img.above_fold = (r.get("top") or 0) < viewport_h

    # ------------------------------------------------------------------ status checks
    async def _check_many(self, urls: list[str], concurrency: int = 6) -> None:
        sem = asyncio.Semaphore(concurrency)

        async def one(u: str) -> None:
            async with sem:
                await self._check_cancel()
                if self.scope.is_internal(u) and not self.robots.can_fetch(u, self.ua_token):
                    self.url_status[u] = UrlStatus(url=u, status=None, blocked_by_robots=True)
                    return
                r = await self.fetcher.check(u)
                self.url_status[u] = UrlStatus(url=u, status=r.status, final_url=r.final_url,
                                               redirects=len(r.chain), error=r.error,
                                               blocked_by_robots=r.blocked_redirect)
                await self._report("links", message=f"Checked {u}")

        await asyncio.gather(*(one(u) for u in urls))

    async def _check_uncrawled(self, sitemap_urls: dict[str, str | None]) -> None:
        internal: Counter[str] = Counter()
        external: Counter[str] = Counter()
        for p in list(self.pages.values()):
            for l in p.links:
                if l.target in self.pages or l.target in self.url_status:
                    continue
                (internal if l.internal else external)[l.target] += 1
            # canonical and hreflang targets must be verified too
            extra = [p.canonical_target] + [h.get("href") for h in p.hreflang]
            for t in extra:
                if t and t not in self.pages and t not in self.url_status and self.scope.is_internal(t):
                    internal[t] += 1000  # check these first
        todo = [u for u, _ in internal.most_common(self.cfg.max_internal_link_checks)]
        sm_left = [u for u in sitemap_urls if u not in self.pages and u not in self.url_status
                   and u not in internal][: self.cfg.sitemap_check_limit]
        await self._check_many(todo + sm_left)
        await self._check_many([u for u, _ in external.most_common(self.cfg.max_external_link_checks)], 8)

    async def _probe_images(self) -> dict[str, ImageAsset]:
        counts: Counter[str] = Counter()
        for p in self.pages.values():
            for img in p.images:
                counts[img.src] += 1
        srcs = [s for s, _ in counts.most_common(self.cfg.max_image_checks)]
        out: dict[str, ImageAsset] = {}
        sem = asyncio.Semaphore(6)

        async def one(src: str) -> None:
            async with sem:
                await self._check_cancel()
                if self.scope.is_internal(src) and not self.robots.can_fetch(src, self.ua_token):
                    out[src] = ImageAsset(src=src, error="blocked by robots.txt")
                    return
                r = await probe_image(self.fetcher, src)
                out[src] = ImageAsset(src=src, status=r.status, bytes=r.bytes, bytes_exact=r.bytes_exact,
                                      format=r.format, intrinsic_w=r.width, intrinsic_h=r.height,
                                      content_type=r.content_type, error=r.error)
                await self._report("images", message=f"Image {len(out)}/{len(srcs)}")

        await asyncio.gather(*(one(s) for s in srcs))
        return out

    async def _render_samples(self) -> None:
        """Render one page per common template (with images) to learn image display sizes."""
        n = self.cfg.render_sample_templates
        if n <= 0 or self.cfg.render_mode == "never" or self.renderer.unavailable:
            return
        from app.crawler.urls import template_key
        groups: dict[str, list[PageData]] = {}
        for p in self.pages.values():
            if p.is_ok and p.images and not any(i.rendered_w for i in p.images):
                groups.setdefault(template_key(p.url), []).append(p)
        ordered = sorted(groups.values(), key=len, reverse=True)[:n]
        for i, group in enumerate(ordered):
            await self._check_cancel()
            await self._report("render", force=True, message=f"Measuring image display sizes ({i + 1}/{len(ordered)})")
            page = group[0]
            rr = await self.renderer.render(page.url, load_images=True)
            if rr.images and not rr.error:
                self._apply_image_rects(page, rr.images, rr.viewport_h)

    async def _probe_variants(self, final: str, res: FetchResult, https_error: str | None) -> tuple[dict, dict]:
        f = self.fetcher
        parts = urlsplit(final)
        host = parts.hostname or ""
        https: dict = {"start_scheme": parts.scheme, "https_error": https_error,
                       "hsts": res.headers.get("strict-transport-security")}
        variants: dict = {}
        if parts.scheme == "https":
            r = await f.check(f"http://{parts.netloc}/", retries=0)
            https["http_redirects_to_https"] = bool(r.final_url and r.final_url.startswith("https://"))
            https["http_probe"] = {"status": r.status, "final_url": r.final_url, "hops": len(r.chain), "error": r.error}
        else:
            r = await f.check(f"https://{parts.netloc}/", retries=0)
            https["https_available"] = r.status is not None and r.status < 400
            https["https_probe"] = {"status": r.status, "final_url": r.final_url, "error": r.error}
        is_ip = host.replace(".", "").isdigit() or ":" in host or host == "localhost"
        alt_host = toggle_www(host)
        r = await f.check(f"{parts.scheme}://{alt_host}/", retries=0) if not is_ip else None
        variants["www"] = {"skipped": "IP/localhost host"} if r is None else {
            "variant": f"{parts.scheme}://{alt_host}/", "status": r.status, "final_url": r.final_url,
            "error": r.error, "resolves": r.status is not None,
            "consolidated": r.status is None or host_of(r.final_url or "") == host or (r.status or 0) >= 400,
        }
        sample = next((p for p in self.pages.values() if p.is_ok and urlsplit(p.url).path not in ("", "/")
                       and not urlsplit(p.url).query), None)
        if sample:
            sp = urlsplit(sample.url)
            path = sp.path[:-1] if sp.path.endswith("/") else sp.path + "/"
            vurl = urlunsplit((sp.scheme, sp.netloc, path, "", ""))
            r = await f.check(vurl, retries=0)
            dup = r.status == 200 and not r.chain
            variants["trailing_slash"] = {"page": sample.url, "variant": vurl, "status": r.status,
                                          "final_url": r.final_url, "redirects": len(r.chain),
                                          "duplicate": dup, "page_canonical": sample.canonical_target}
        # soft 404: does a URL that cannot exist return 200?
        probe = f"{parts.scheme}://{parts.netloc}/seo-inspector-check-{secrets.token_hex(6)}"
        r = await f.fetch(probe, html_only=True, max_bytes=300_000, retries=0)
        variants["soft_404"] = {"probe": probe, "status": r.status, "final_url": r.final_url,
                                "redirects": len(r.chain), "error": r.error,
                                "same_as_homepage": bool(r.body) and r.body == res.body[:300_000]}
        return https, variants


async def crawl_site(input_url: str, cfg: CrawlSettings, user_agent: str, **kw) -> SiteModel:
    return await Crawler(input_url, cfg, user_agent, **kw).run()
