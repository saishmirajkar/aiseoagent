"""The Site Model: everything the crawler learns, built once and shared by all modules."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class LinkRec:
    target: str  # absolute, normalized
    anchor: str
    internal: bool
    nofollow: bool = False
    sponsored: bool = False
    ugc: bool = False
    image_link: bool = False  # anchor came from <img alt>


@dataclass
class ImageRec:
    src: str  # absolute, normalized
    alt: str | None  # None = attribute missing, "" = empty (decorative)
    filename: str
    width_attr: int | None = None
    height_attr: int | None = None
    loading: str | None = None
    position: int = 0  # order on the page
    has_srcset: bool = False
    modern_source: bool = False  # <picture><source type="image/webp|avif">
    in_link: bool = False
    rendered_w: int | None = None
    rendered_h: int | None = None
    above_fold: bool | None = None


@dataclass
class ImageAsset:
    src: str
    status: int | None = None
    bytes: int | None = None
    bytes_exact: bool = False
    format: str | None = None
    intrinsic_w: int | None = None
    intrinsic_h: int | None = None
    content_type: str | None = None
    error: str | None = None


@dataclass
class PageData:
    url: str
    final_url: str
    status: int | None = None
    redirect_chain: list[dict] = field(default_factory=list)  # [{url, status, location}]
    redirect_loop: bool = False
    response_ms: int | None = None
    content_type: str = ""
    is_html: bool = False
    error: str | None = None
    depth: int | None = None
    discovered_via: str = "link"  # start | link | sitemap
    blocked_by_robots: bool = False
    # rendering
    rendered: bool = False
    js_dependent: bool = False
    raw_signals: dict[str, Any] | None = None  # pre-render values when rendered
    # head
    title: str | None = None
    title_count: int = 0
    meta_description: str | None = None
    description_count: int = 0
    meta_robots: str | None = None
    x_robots_tag: str | None = None
    canonical: str | None = None
    canonicals: list[str] = field(default_factory=list)
    canonical_header: str | None = None
    canonical_in_body: bool = False
    hreflang: list[dict] = field(default_factory=list)  # [{lang, href}]
    lang: str | None = None
    viewport: str | None = None
    meta_refresh: str | None = None
    og: dict[str, str] = field(default_factory=dict)
    twitter: dict[str, str] = field(default_factory=dict)
    # body
    headings: list[tuple[int, str]] = field(default_factory=list)
    text: str = ""
    word_count: int = 0
    content_hash: str | None = None
    simhash: int | None = None
    links: list[LinkRec] = field(default_factory=list)
    images: list[ImageRec] = field(default_factory=list)
    structured_data: dict[str, Any] = field(default_factory=dict)  # {jsonld, microdata, rdfa, errors}
    nap: dict[str, Any] = field(default_factory=dict)
    authors: list[str] = field(default_factory=list)
    dates: dict[str, str] = field(default_factory=dict)
    mixed_content: list[str] = field(default_factory=list)
    resources: list[str] = field(default_factory=list)  # same-site CSS/JS (robots checks)
    # computed by the builder
    in_sitemap: bool = False
    inlinks: int = 0
    inlink_sources: list[str] = field(default_factory=list)
    template_key: str | None = None

    # ------------------------------------------------------------------ derived
    @property
    def robots_directives(self) -> set[str]:
        out: set[str] = set()
        for v in (self.meta_robots, self.x_robots_tag):
            if v:
                for part in v.lower().replace(";", ",").split(","):
                    part = part.strip()
                    if ":" in part and not part.startswith(("max-", "unavailable_after")):
                        part = part.split(":", 1)[1].strip()  # "googlebot: noindex"
                    if part:
                        out.add(part)
        return out

    @property
    def is_noindex(self) -> bool:
        d = self.robots_directives
        return "noindex" in d or "none" in d

    @property
    def is_ok(self) -> bool:
        return self.status == 200 and not self.redirect_chain and self.is_html and not self.error

    @property
    def canonical_target(self) -> str | None:
        return self.canonical or self.canonical_header

    @property
    def canonical_is_self(self) -> bool:
        c = self.canonical_target
        return c is None or c == self.url

    @property
    def indexable(self) -> bool:
        return self.is_ok and not self.is_noindex and not self.blocked_by_robots and self.canonical_is_self

    def to_json(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("text", None)
        d["links"] = len(self.links)
        d["images"] = len(self.images)
        return d


@dataclass
class SiteFacts:
    input_url: str
    start_url: str
    origin: str
    host: str
    robots: dict[str, Any] = field(default_factory=dict)
    ai_bots: dict[str, Any] = field(default_factory=dict)
    sitemaps: list[dict] = field(default_factory=list)
    sitemap_found: bool = False
    sitemap_url_count: int = 0
    sitemap_truncated: bool = False
    llms_txt: dict[str, Any] = field(default_factory=dict)
    https: dict[str, Any] = field(default_factory=dict)
    variants: dict[str, Any] = field(default_factory=dict)
    js_site: bool = False
    crawl: dict[str, Any] = field(default_factory=dict)


@dataclass
class UrlStatus:
    """Status of a URL that was checked but not crawled as a page (links, sitemap entries)."""
    url: str
    status: int | None
    final_url: str | None = None
    redirects: int = 0
    error: str | None = None
    blocked_by_robots: bool = False


@dataclass
class SiteModel:
    facts: SiteFacts
    pages: dict[str, PageData] = field(default_factory=dict)
    images: dict[str, ImageAsset] = field(default_factory=dict)
    url_status: dict[str, UrlStatus] = field(default_factory=dict)  # extra checked URLs
    sitemap_urls: dict[str, str | None] = field(default_factory=dict)
    robots: Any = None  # app.crawler.robots.RobotsInfo (runtime only)
    duplicate_groups: list[list[str]] = field(default_factory=list)  # exact + near duplicates

    # --------------------------------------------------------------- helpers
    def html_pages(self) -> list[PageData]:
        return [p for p in self.pages.values() if p.is_ok]

    def indexable_pages(self) -> list[PageData]:
        return [p for p in self.pages.values() if p.indexable]

    def status_of(self, url: str) -> UrlStatus | None:
        """Best-known status for any URL (crawled page or separately checked)."""
        p = self.pages.get(url)
        if p is not None:
            return UrlStatus(url=url, status=p.status, final_url=p.final_url,
                             redirects=len(p.redirect_chain), error=p.error,
                             blocked_by_robots=p.blocked_by_robots)
        return self.url_status.get(url)

    def final_status_of(self, url: str) -> int | None:
        """Status after following redirects (looks up the redirect target when crawled)."""
        s = self.status_of(url)
        if s is None:
            return None
        if s.redirects and s.final_url and s.final_url != url:
            t = self.status_of(s.final_url)
            if t is not None and not t.redirects:
                return t.status
        return s.status

    @property
    def homepage(self) -> PageData | None:
        return self.pages.get(self.facts.start_url) or next(
            (p for p in self.pages.values() if p.depth == 0 and p.is_ok), None)
