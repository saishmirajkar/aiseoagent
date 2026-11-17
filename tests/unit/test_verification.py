import asyncio

from app.crawler.fetcher import FetchResult
from app.modules.common import is_own_error_page, resource_kind
from app.modules.m06_technical import TechnicalAudit
from app.sitemodel.types import UrlStatus
from tests.unit.helpers import BASE, ctx, link, page, run, site


def test_resource_kind_and_error_page():
    assert resource_kind(BASE + "/about") == "page"
    assert resource_kind(BASE + "/pdf/oct.pdf") == "PDF file"
    assert resource_kind(BASE + "/x", "image/png") == "image"
    assert resource_kind(BASE + "/app.js") == "script/style file"
    assert is_own_error_page(BASE + "/404") and is_own_error_page(BASE + "/404.html")
    assert not is_own_error_page(BASE + "/products/404-kart")


def test_status_checks_skip_error_page_and_bot_blocks():
    home = page("/", links=[link("/gone"), link("/protected"), link("/file.pdf")])
    s = site([home, page("/404", status=404), page("/gone", status=404, rechecked=True)],
             url_status={BASE + "/protected": UrlStatus(BASE + "/protected", 403),
                         BASE + "/file.pdf": UrlStatus(BASE + "/file.pdf", 404, content_type="application/pdf")})
    r = run(TechnicalAudit(), ctx(s))
    four = {i.url: i.evidence for i in r["tech.status_4xx"].items}
    assert set(four) == {BASE + "/gone", BASE + "/file.pdf"}  # /404 is the site's own error page; 403 not counted
    assert four[BASE + "/gone"]["confirmed"] == "failed twice" and four[BASE + "/file.pdf"]["type"] == "PDF file"
    fe = r["tech.fetch_errors"]
    assert [i.url for i in fe.items] == [BASE + "/protected"] and "refused our checker" in fe.summary
    bil = {i.url: i.evidence for i in r["tech.broken_internal_links"].items}
    assert {b["link"] for b in bil[BASE + "/"]["broken_links"]} == {BASE + "/gone", BASE + "/file.pdf"}


def test_verify_errors_recovers_transient_failures():
    from app.crawler.crawl import Crawler
    from app.crawler.urls import SiteScope

    class FakeFetcher:
        async def fetch(self, url, **kw):
            ok = url.endswith("/flaky")
            return FetchResult(url=url, final_url=url, status=200 if ok else 404, chain=[],
                               headers={"content-type": "text/html"} if ok else {}, body=b"<html><title>t</title></html>" if ok else b"")

    c = Crawler.__new__(Crawler)
    c.scope, c.fetcher = SiteScope("ex.com"), FakeFetcher()
    c.pages = {BASE + "/flaky": page("/flaky", status=503, is_html=False)}
    c.url_status = {BASE + "/dead": UrlStatus(BASE + "/dead", 404)}
    c.seen, c.robots, c.renderer = set(), None, None

    async def ingest(u, res, depth, via, q, prio=0):
        c.pages[u] = page(u.replace(BASE, ""), status=res.status)
    c._ingest = ingest
    asyncio.run(c._verify_errors(pause_s=0))
    assert c.pages[BASE + "/flaky"].status == 200
    assert c.url_status[BASE + "/dead"].rechecked is True


def test_corrected_jsonld_fills_from_page_and_marks_placeholders():
    import json
    from app.modules.m09_schema import SchemaMarkup, corrected_jsonld
    p = page("/kart", title="Genesis S25 | KnK", headings=[(1, "Genesis S25")], meta_description="Rental kart.",
             og={"image": BASE + "/k.jpg"})
    fix = corrected_jsonld({"@context": "https://schema.org", "@type": "Product", "offers": {"@type": "Offer"}}, p)
    data = json.loads(fix["code"].split("\n", 1)[1].rsplit("\n", 1)[0])
    assert data["name"] == "Genesis S25" and data["image"] == BASE + "/k.jpg" and data["description"] == "Rental kart."
    assert data["offers"]["price"].startswith("REPLACE:") and "offers[0].price" in fix["needs_input"]
    assert corrected_jsonld({"@context": "https://schema.org", "@type": "WebSite", "name": "x", "url": "y"}, p) is None
    p.structured_data = {"jsonld": [{"@context": "https://schema.org", "@type": "Product", "name": "Genesis S25"}]}
    r = run(SchemaMarkup(), ctx(site([page("/"), p])))
    item = next(i for i in r["schema.required"].items if i.url == p.url)
    assert item.generated_fix and "REPLACE:" in item.generated_fix["code"]


def test_orphans_ignore_non_html_sitemap_entries():
    from app.modules.m04_internal_links import InternalLinking
    sm = {BASE + "/": None, BASE + "/lonely": None, BASE + "/img/a.png": None, BASE + "/doc.pdf": None}
    s = site([page("/", links=[]), page("/lonely")], sitemap=sm,
             url_status={BASE + "/img/a.png": UrlStatus(BASE + "/img/a.png", 200, content_type="image/png"),
                         BASE + "/doc.pdf": UrlStatus(BASE + "/doc.pdf", 200)})
    r = run(InternalLinking(), ctx(s))["links.orphans"]
    assert {i.url for i in r.items} == {BASE + "/lonely"} and "non-HTML" in r.summary


def test_fact_consistency_catches_knk_style_contradictions():
    from app.modules.m17_geo import extract_fact_claims, fact_problems
    home = page("/", text="KnK Karts has been at the forefront since 1999. With 25+ years of expertise. "
                          "About Us Year of establishment 1599 Total awards 8 + Years of experience 10 +")
    claims = extract_fact_claims([home])
    assert {c["value"] for c in claims["founded"]} == {1999, 1599}
    assert {c["value"] for c in claims["experience"]} == {25, 10}
    probs = [p["problem"] for p in fact_problems(claims, 2026)]
    assert any("Implausible founding year 1599" in p for p in probs)
    assert any(p.startswith("10 years of experience, but founded in 1999") for p in probs)
    assert not any(p.startswith("25 years") for p in probs)  # 25+ matches 1999
    ok = extract_fact_claims([page("/", text="Founded in 2010. 15 years of experience.")])
    assert fact_problems(ok, 2026) == []
