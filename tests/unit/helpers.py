"""Builders for hand-made Site Models used by check-module unit tests."""
from __future__ import annotations

import asyncio

from app.config import RuntimeSettings
from app.modules.base import AuditContext, AuditInput, CheckModule, CheckResult
from app.sitemodel.builder import build_model
from app.sitemodel.types import ImageAsset, ImageRec, LinkRec, PageData, SiteFacts, SiteModel, UrlStatus

BASE = "https://ex.com"


def page(path: str = "/", **kw) -> PageData:
    url = BASE + path
    defaults = dict(url=url, final_url=url, status=200, is_html=True, content_type="text/html",
                    title="A perfectly reasonable page title for tests", meta_description="d" * 140,
                    viewport="width=device-width, initial-scale=1", lang="en", canonical=url,
                    canonicals=[url], word_count=300, response_ms=100,
                    og={"title": "t", "description": "d", "image": "i"})
    defaults.update(kw)
    return PageData(**defaults)


def link(path_or_url: str, internal: bool | None = None, **kw) -> LinkRec:
    url = path_or_url if "://" in path_or_url else BASE + path_or_url
    return LinkRec(target=url, anchor=kw.pop("anchor", "anchor"),
                   internal=internal if internal is not None else url.startswith(BASE), **kw)


def img(src: str, **kw) -> ImageRec:
    url = src if "://" in src else BASE + src
    return ImageRec(src=url, alt=kw.pop("alt", "An image"), filename=url.rsplit("/", 1)[-1],
                    width_attr=kw.pop("width_attr", 100), height_attr=kw.pop("height_attr", 100), **kw)


def asset(src: str, **kw) -> ImageAsset:
    url = src if "://" in src else BASE + src
    return ImageAsset(src=url, status=kw.pop("status", 200), **kw)


def site(pages: list[PageData], *, url_status: dict[str, UrlStatus] | None = None,
         images: dict[str, ImageAsset] | None = None, sitemap: dict | None = None, robots=None,
         **facts) -> SiteModel:
    f = SiteFacts(input_url=BASE + "/", start_url=BASE + "/", origin=BASE, host="ex.com")
    for k, v in facts.items():
        setattr(f, k, v)
    m = SiteModel(facts=f, pages={p.url: p for p in pages}, images=images or {}, url_status=url_status or {},
                  sitemap_urls=sitemap or {}, robots=robots)
    return build_model(m)


def offline_settings() -> RuntimeSettings:
    """Defaults, minus sources that call live third-party endpoints (Trends, Autocomplete)."""
    s = RuntimeSettings()
    s.data.trends_enabled = False
    s.data.autocomplete_seeds = 0
    return s


def ctx(s: SiteModel, settings: RuntimeSettings | None = None, integrations: dict | None = None) -> AuditContext:
    return AuditContext(audit_id=None, site=s, settings=settings or offline_settings(),
                        inputs=AuditInput(url=BASE + "/"), integrations=integrations or {})


def run(module: CheckModule, c: AuditContext) -> dict[str, CheckResult]:
    results = asyncio.run(module.run(c))
    assert len({r.spec.id for r in results}) == len(module.checks), "each check must report exactly once"
    return {r.spec.id: r for r in results}


def urls(r: CheckResult) -> set[str]:
    return {i.url for i in r.items}
