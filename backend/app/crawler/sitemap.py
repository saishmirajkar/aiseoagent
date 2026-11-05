"""XML sitemap discovery and streaming parsing (handles indexes and .gz)."""
from __future__ import annotations

import zlib
from dataclasses import dataclass, field

from lxml import etree

from app.crawler.fetcher import Fetcher
from app.crawler.urls import normalize_url

MAX_SITEMAP_FILES = 50
MAX_UNCOMPRESSED = 60_000_000  # protocol limit is 50 MB uncompressed
FALLBACK_PATHS = ["/sitemap.xml", "/sitemap_index.xml", "/sitemap-index.xml", "/wp-sitemap.xml"]


@dataclass
class SitemapFile:
    url: str
    status: int | None
    kind: str | None = None  # urlset | sitemapindex
    url_count: int = 0
    error: str | None = None
    source: str = "robots"  # robots | fallback | index
    truncated: bool = False  # stopped at the URL limit

    def as_dict(self) -> dict:
        return self.__dict__.copy()


@dataclass
class SitemapResult:
    files: list[SitemapFile] = field(default_factory=list)
    urls: dict[str, str | None] = field(default_factory=dict)  # url -> lastmod
    truncated: bool = False

    @property
    def found(self) -> bool:
        return any(f.status == 200 and f.kind and not f.error for f in self.files)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


class _StreamParser:
    """Incremental parser: feeds bytes, collects <loc>/<lastmod> without building a tree."""

    def __init__(self, max_urls: int):
        self.parser = etree.XMLPullParser(events=("start", "end"), resolve_entities=False,
                                          no_network=True, huge_tree=False)
        self.kind: str | None = None
        self.locs: list[tuple[str, str | None]] = []
        self.max_urls = max_urls
        self._lastmod: str | None = None
        self._loc: str | None = None
        self.full = False

    def feed(self, data: bytes) -> None:
        self.parser.feed(data)
        self._drain()

    def close(self) -> None:
        self.parser.close()
        self._drain()

    def _drain(self) -> None:
        for event, el in self.parser.read_events():
            name = _local(el.tag)
            if event == "start":
                if self.kind is None:
                    self.kind = name
                continue
            if name == "loc":
                self._loc = (el.text or "").strip()
            elif name == "lastmod":
                self._lastmod = (el.text or "").strip() or None
            elif name in ("url", "sitemap"):
                if self._loc and len(self.locs) < self.max_urls:
                    self.locs.append((self._loc, self._lastmod))
                elif self._loc:
                    self.full = True
                self._loc, self._lastmod = None, None
                el.clear()
                parent = el.getparent()
                if parent is not None:
                    while el.getprevious() is not None:
                        del parent[0]


async def _parse_one(fetcher: Fetcher, url: str, max_urls: int, source: str
                     ) -> tuple[SitemapFile, list[tuple[str, str | None]]]:
    sf = SitemapFile(url=url, status=None, source=source)
    parser = _StreamParser(max_urls)
    gz: zlib._Decompress | None = None
    total = 0
    first = True
    async with fetcher.stream(url) as (res, body):
        sf.status = res.status
        if res.error or body is None:
            sf.error = res.error or "redirect problem"
            return sf, []
        if res.status != 200:
            sf.error = f"HTTP {res.status}"
            return sf, []
        try:
            async for chunk in body:
                if first:
                    first = False
                    if chunk[:2] == b"\x1f\x8b":
                        gz = zlib.decompressobj(16 + zlib.MAX_WBITS)
                data = gz.decompress(chunk) if gz else chunk
                total += len(data)
                if total > MAX_UNCOMPRESSED:
                    sf.error = "sitemap exceeds 50 MB uncompressed"
                    break
                parser.feed(data)
                if parser.full:
                    break
            if not sf.error and not parser.full:
                parser.close()
        except etree.XMLSyntaxError as e:
            sf.error = f"Invalid XML: {e}"[:300]
        except zlib.error as e:
            sf.error = f"Invalid gzip: {e}"
    sf.kind = parser.kind
    if not sf.error and parser.kind not in ("urlset", "sitemapindex"):
        sf.error = f"Unexpected root element <{parser.kind}>" if parser.kind else "Empty or non-XML response"
    sf.url_count = len(parser.locs)
    sf.truncated = parser.full
    return sf, parser.locs


async def collect_sitemaps(fetcher: Fetcher, origin: str, robots_sitemaps: list[str],
                           max_urls: int) -> SitemapResult:
    result = SitemapResult()
    queue: list[tuple[str, str]] = []
    for s in robots_sitemaps:
        n = normalize_url(s, origin)
        if n:
            queue.append((n, "robots"))
    fallback = not queue
    if fallback:
        queue = [(origin + p, "fallback") for p in FALLBACK_PATHS]
    seen: set[str] = set()
    while queue and len(seen) < MAX_SITEMAP_FILES:
        url, source = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        remaining = max(0, max_urls - len(result.urls))
        sf, locs = await _parse_one(fetcher, url, remaining or 1, source)
        if source == "fallback" and sf.status != 200:
            continue  # only report fallback probes that exist
        result.files.append(sf)
        if sf.truncated and sf.kind == "urlset":
            result.truncated = True
        if sf.kind == "sitemapindex":
            for loc, _ in locs:
                n = normalize_url(loc, url)
                if n and n not in seen:
                    queue.append((n, "index"))
        elif sf.kind == "urlset":
            for loc, lastmod in locs:
                n = normalize_url(loc, url)
                if n:
                    if len(result.urls) >= max_urls:
                        result.truncated = True
                        break
                    result.urls.setdefault(n, lastmod)
        if fallback and sf.status == 200 and sf.kind:
            queue = [q for q in queue if q[1] != "fallback"]
    return result
