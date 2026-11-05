"""Helpers shared by check modules (link index, fix generators, importance)."""
from __future__ import annotations

import re
from collections import defaultdict
from urllib.parse import urlsplit

from app.modules.base import AuditContext
from app.sitemodel.types import PageData, SiteModel

UTILITY_PATH = re.compile(
    r"/(cart|basket|checkout|my-?account|account|login|log-in|signin|sign-in|register|signup|"
    r"search|wp-admin|wp-login|admin|thank-?you|thanks|feed|tag|author|print)(/|$|\?)", re.I)

MAX_ITEMS = 500


def link_sources(site: SiteModel) -> dict[str, list[str]]:
    """target URL -> pages linking to it (crawled OK pages only)."""
    out: dict[str, set[str]] = defaultdict(set)
    for p in site.pages.values():
        if not p.is_ok:
            continue
        for l in p.links:
            out[l.target].add(p.url)
    return {k: sorted(v) for k, v in out.items()}


def cached_link_sources(ctx: AuditContext) -> dict[str, list[str]]:
    if "link_sources" not in ctx.shared:
        ctx.shared["link_sources"] = link_sources(ctx.site)
    return ctx.shared["link_sources"]


def internal_targets(ctx: AuditContext) -> set[str]:
    if "internal_targets" not in ctx.shared:
        ctx.shared["internal_targets"] = {l.target for p in ctx.site.pages.values() if p.is_ok
                                          for l in p.links if l.internal}
    return ctx.shared["internal_targets"]


def is_utility(url: str) -> bool:
    return bool(UTILITY_PATH.search(urlsplit(url).path + ("?" + urlsplit(url).query if urlsplit(url).query else "")))


def is_important(page: PageData, site: SiteModel, min_inlinks: int) -> bool:
    """Homepage, pages linked from the homepage, pages in the sitemap, or widely linked pages."""
    if is_utility(page.url):
        return False
    if page.url == site.facts.start_url or page.depth == 0:
        return True
    html_count = max(1, len(site.html_pages()))
    return (page.depth == 1 or page.in_sitemap
            or page.inlinks >= max(min_inlinks, int(0.5 * html_count)))


def path_of(url: str) -> str:
    p = urlsplit(url)
    return p.path + (f"?{p.query}" if p.query else "")


def redirect_fix(src: str, dst: str) -> dict:
    path = path_of(src)
    return {
        "type": "redirect_rule", "from": src, "to": dst,
        "apache": f"Redirect 301 {path} {dst}",
        "nginx": f"location = {urlsplit(src).path} {{ return 301 {dst}; }}",
    }


def html_tag_fix(tag: str, where: str = "<head>") -> dict:
    return {"type": "html", "where": where, "code": tag}


def esc_attr(s: str) -> str:
    return s.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;")


def truncate(items: list, limit: int = MAX_ITEMS) -> list:
    return items[:limit]
