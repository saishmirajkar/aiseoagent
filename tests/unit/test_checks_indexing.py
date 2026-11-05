from app.crawler.robots import build_robots
from app.modules.base import Status
from app.modules.m07_indexing import CrawlingIndexing
from app.sitemodel.types import UrlStatus

from tests.unit.helpers import BASE, ctx, link, page, run, site, urls

M = CrawlingIndexing()
ROBOTS_OK = {"url": BASE + "/robots.txt", "status": 200, "sitemaps": [BASE + "/sitemap.xml"], "size": 10}
SM_FILES = [{"url": BASE + "/sitemap.xml", "status": 200, "kind": "urlset", "url_count": 3, "source": "robots"}]


def robots(text="User-agent: *\nDisallow:\n"):
    return build_robots(BASE + "/robots.txt", 200, text)


def test_healthy_site():
    s = site([page("/", links=[link("/a")]), page("/a")], robots=robots(),
             sitemap={BASE + "/": None, BASE + "/a": None},
             robots_txt=None)
    s.facts.robots, s.facts.sitemaps, s.facts.sitemap_found = ROBOTS_OK, SM_FILES, True
    r = run(M, ctx(s))
    for cid in ("crawl.robots_txt", "crawl.robots_blocks_important", "crawl.robots_blocks_resources",
                "crawl.robots_sitemap_directive", "crawl.sitemap_present", "crawl.sitemap_errors",
                "crawl.sitemap_bad_urls", "crawl.sitemap_non_indexable", "crawl.sitemap_missing_pages",
                "crawl.noindex_linked"):
        assert r[cid].status == Status.PASS, (cid, r[cid].summary)
    assert r["crawl.gsc_index_coverage"].status == Status.NOT_CHECKED
    assert "Search Console" in r["crawl.gsc_index_coverage"].reason


def test_robots_missing_and_unreachable():
    s = site([page("/")])
    s.facts.robots = {"url": BASE + "/robots.txt", "status": 404}
    r = run(M, ctx(s))
    assert r["crawl.robots_txt"].status == Status.WARNING and r["crawl.robots_txt"].generated_fix
    s.facts.robots = {"url": BASE + "/robots.txt", "status": 503}
    assert run(M, ctx(s))["crawl.robots_txt"].status == Status.FAIL


def test_robots_blocks_important_urls_and_resources():
    rb = robots("User-agent: *\nDisallow: /services\nDisallow: /static/\n")
    pages = [page("/", links=[link("/services")], resources=[BASE + "/static/app.js"])]
    pages += [page(f"/p{i}", links=[link("/services")]) for i in range(3)]
    pages[0].links += [link(f"/p{i}") for i in range(3)]
    s = site(pages, robots=rb)
    s.facts.robots = ROBOTS_OK
    r = run(M, ctx(s))
    assert urls(r["crawl.robots_blocks_important"]) == {BASE + "/services"}
    assert r["crawl.robots_blocks_resources"].status == Status.WARNING


def test_sitemap_missing_generates_xml():
    s = site([page("/", links=[link("/a")]), page("/a")])
    s.facts.robots = ROBOTS_OK
    r = run(M, ctx(s))
    sp = r["crawl.sitemap_present"]
    assert sp.status == Status.FAIL
    assert "<loc>https://ex.com/a</loc>" in sp.generated_fix["content"]
    assert r["crawl.sitemap_bad_urls"].status == Status.NOT_APPLICABLE


def test_sitemap_url_quality():
    sm = {BASE + "/": None, BASE + "/gone": None, BASE + "/moved": None, BASE + "/hidden": None,
          BASE + "/canon": None, "https://elsewhere.com/x": None}
    s = site([page("/", links=[link("/extra")]), page("/gone", status=404), page("/hidden", meta_robots="noindex"),
              page("/canon", canonical=BASE + "/", canonicals=[BASE + "/"]), page("/extra")],
             url_status={BASE + "/moved": UrlStatus(BASE + "/moved", 301, BASE + "/", redirects=1)},
             sitemap=sm)
    s.facts.robots, s.facts.sitemaps, s.facts.sitemap_found = ROBOTS_OK, SM_FILES, True
    r = run(M, ctx(s))
    bad = r["crawl.sitemap_bad_urls"]
    assert bad.status == Status.FAIL
    assert {BASE + "/gone", BASE + "/moved", "https://elsewhere.com/x"} <= urls(bad)
    assert urls(r["crawl.sitemap_non_indexable"]) == {BASE + "/hidden", BASE + "/canon"}
    assert urls(r["crawl.sitemap_missing_pages"]) == {BASE + "/extra"}


def test_robots_without_sitemap_line():
    s = site([page("/")])
    s.facts.robots = {**ROBOTS_OK, "sitemaps": []}
    r = run(M, ctx(s))
    assert r["crawl.robots_sitemap_directive"].status == Status.WARNING
    assert r["crawl.robots_sitemap_directive"].generated_fix["code"].startswith("Sitemap:")
