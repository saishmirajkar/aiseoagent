from app.modules.base import Status
from app.modules.m06_technical import TechnicalAudit
from app.sitemodel.types import UrlStatus

from tests.unit.helpers import BASE, ctx, link, page, run, site, urls

M = TechnicalAudit()


def home(**kw):
    return page("/", **kw)


def test_clean_site_passes():
    s = site([home(links=[link("/a")]), page("/a", links=[link("/")])],
             https={"start_scheme": "https", "http_redirects_to_https": True},
             variants={"www": {"resolves": True, "consolidated": True}})
    r = run(M, ctx(s))
    for cid in ("tech.status_4xx", "tech.status_5xx", "tech.broken_internal_links", "tech.redirect_chains",
                "tech.redirect_loops", "tech.canonical_missing", "tech.canonical_broken", "tech.canonical_conflict",
                "tech.noindex_important", "tech.https", "tech.mixed_content", "tech.url_variants", "tech.viewport",
                "tech.lang", "tech.slow_response", "tech.meta_refresh"):
        assert r[cid].status == Status.PASS, (cid, r[cid].summary, r[cid].items)
    assert r["tech.hreflang"].status == Status.NOT_APPLICABLE
    assert r["tech.js_dependent"].status == Status.NOT_CHECKED  # nothing rendered


def test_status_errors_and_broken_links():
    s = site([home(links=[link("/gone"), link("/err"), link("https://other.com/x"), link("https://other.com/bot")]),
              page("/gone", status=404), page("/err", status=500)],
             url_status={"https://other.com/x": UrlStatus("https://other.com/x", 404),
                         "https://other.com/bot": UrlStatus("https://other.com/bot", 403)})
    r = run(M, ctx(s))
    assert r["tech.status_4xx"].status == Status.FAIL and urls(r["tech.status_4xx"]) == {BASE + "/gone"}
    assert urls(r["tech.status_5xx"]) == {BASE + "/err"}
    bil = r["tech.broken_internal_links"]
    assert bil.status == Status.FAIL and bil.items[0].evidence["count"] == 2
    ext = r["tech.broken_external_links"]
    assert ext.status == Status.WARNING and ext.affected == 1  # 403 is not counted as broken


def test_uncrawled_internal_link_status():
    s = site([home(links=[link("/deep")])], url_status={BASE + "/deep": UrlStatus(BASE + "/deep", 410)})
    r = run(M, ctx(s))
    assert BASE + "/deep" in urls(r["tech.status_4xx"])


def test_redirect_chain_loop_and_links_to_redirects():
    chain = [{"url": BASE + "/old", "status": 301, "location": BASE + "/mid"},
             {"url": BASE + "/mid", "status": 301, "location": BASE + "/new"}]
    s = site([home(links=[link("/old"), link("/loop")]),
              page("/old", status=301, redirect_chain=chain, final_url=BASE + "/new", is_html=False),
              page("/new"),
              page("/loop", status=301, redirect_loop=True, is_html=False,
                   redirect_chain=[{"url": BASE + "/loop", "status": 301, "location": BASE + "/loop2"}])])
    r = run(M, ctx(s))
    ch = r["tech.redirect_chains"]
    assert ch.status == Status.WARNING and urls(ch) == {BASE + "/old"}
    assert ch.items[0].generated_fix["apache"] == f"Redirect 301 /old {BASE}/new"
    assert urls(r["tech.redirect_loops"]) == {BASE + "/loop"}
    assert r["tech.links_to_redirects"].status == Status.WARNING


def test_canonical_checks():
    s = site([home(links=[link("/a"), link("/b"), link("/c")]),
              page("/a", canonical=None, canonicals=[]),
              page("/b", canonical=BASE + "/gone", canonicals=[BASE + "/gone"]),
              page("/c", canonicals=[BASE + "/c", BASE + "/other"]),
              page("/gone", status=404)])
    r = run(M, ctx(s))
    miss = r["tech.canonical_missing"]
    assert urls(miss) == {BASE + "/a"} and 'rel="canonical"' in miss.items[0].generated_fix["code"]
    assert urls(r["tech.canonical_broken"]) == {BASE + "/b"}
    assert BASE + "/c" in urls(r["tech.canonical_conflict"])


def test_noindex_important_ignores_utility_pages():
    s = site([home(links=[link("/key"), link("/cart")]),
              page("/key", meta_robots="noindex"), page("/cart", meta_robots="noindex")])
    r = run(M, ctx(s))
    assert urls(r["tech.noindex_important"]) == {BASE + "/key"}


def test_hreflang_errors():
    hl = [{"lang": "en", "href": BASE + "/", "valid_code": True},
          {"lang": "de", "href": BASE + "/de/", "valid_code": True},
          {"lang": "english", "href": BASE + "/x", "valid_code": False}]
    s = site([home(hreflang=hl, links=[link("/de/")]), page("/de/", hreflang=[])])
    r = run(M, ctx(s))
    h = r["tech.hreflang"]
    assert h.status == Status.FAIL
    probs = {p["problem"] for p in h.items[0].evidence["problems"]}
    assert {"Invalid language/region codes", "Missing return link"} <= probs


def test_https_and_mixed_content():
    s = site([home(mixed_content=["http://ex.com/a.js"])],
             https={"start_scheme": "https", "http_redirects_to_https": False,
                    "http_probe": {"status": 200, "final_url": "http://ex.com/"}})
    r = run(M, ctx(s))
    assert r["tech.https"].status == Status.FAIL and r["tech.https"].items[0].generated_fix
    assert r["tech.mixed_content"].status == Status.FAIL
    s2 = site([page("/", url="http://ex.com/", final_url="http://ex.com/")], https={"start_scheme": "http"})
    assert run(M, ctx(s2))["tech.https"].status == Status.FAIL


def test_url_variants():
    s = site([home()], variants={"www": {"variant": "https://www.ex.com/", "resolves": True, "consolidated": False,
                                         "status": 200},
                                 "trailing_slash": {"page": BASE + "/a", "variant": BASE + "/a/", "duplicate": True}})
    r = run(M, ctx(s))
    assert r["tech.url_variants"].status == Status.FAIL and r["tech.url_variants"].affected == 2


def test_viewport_lang_slow_meta_refresh():
    s = site([home(links=[link("/a"), link("/b")]), page("/a", viewport=None, lang=None, response_ms=2500),
              page("/b", viewport="initial-scale=1", meta_refresh="0;url=/x")])
    r = run(M, ctx(s))
    assert urls(r["tech.viewport"]) == {BASE + "/a", BASE + "/b"}
    assert urls(r["tech.lang"]) == {BASE + "/a"}
    assert urls(r["tech.slow_response"]) == {BASE + "/a"}
    assert urls(r["tech.meta_refresh"]) == {BASE + "/b"}


def test_js_dependency_and_render_mismatch():
    s = site([home(rendered=True, js_dependent=True, title="New",
                   raw_signals={"title": "Old", "canonical": BASE + "/", "meta_robots": None,
                                "word_count": 5, "internal_links": 0})])
    r = run(M, ctx(s))
    assert r["tech.js_dependent"].status == Status.WARNING
    assert r["tech.render_mismatch"].items[0].evidence["title"] == {"raw": "Old", "rendered": "New"}
