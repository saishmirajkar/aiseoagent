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


def test_action_and_pagination_urls():
    from app.crawler.urls import is_action_url, is_pagination
    assert is_action_url("https://ma.tt/post/?replytocom=1") and is_action_url("https://ma.tt/post/?share=x")
    assert is_action_url("https://shop.com/cart") and is_action_url("https://shop.com/cart/add?id=2")
    assert not is_action_url("https://shop.com/products/cart-wheel") and not is_action_url("https://ma.tt/about/")
    assert is_pagination("https://ma.tt/page/2/") and is_pagination("https://shop.com/collections/all?page=3")
    assert not is_pagination("https://shop.com/collections/all?page=1") and not is_pagination("https://ma.tt/pages/x")


def test_robots_groups_are_merged_and_rule_reported():
    from app.crawler.robots import build_robots
    t = "User-agent: *\nDisallow:\n\nUser-agent: *\nDisallow: /contact\nAllow: /contact/public$\n"
    r = build_robots("https://ma.tt/robots.txt", 200, t)
    assert not r.can_fetch("https://ma.tt/contact/", "Googlebot")
    assert r.matching_rule("https://ma.tt/contact/", "Googlebot") == "Disallow: /contact"
    assert r.matching_rule("https://ma.tt/contact/public", "Googlebot") == "Allow: /contact/public$"


def test_screen_reader_headings_ignored():
    from app.crawler.parser import parse_page
    from app.crawler.urls import SiteScope
    from app.sitemodel.types import PageData
    p = PageData(url=BASE + "/", final_url=BASE + "/")
    parse_page(p, '<html><body><h1>Title</h1><h1 class="screen-reader-text">Post navigation</h1>'
                  '<h2 hidden>x</h2><h2>Real</h2></body></html>', SiteScope("ex.com"))
    assert p.headings == [(1, "Title"), (2, "Real")]


def test_placeholder_filenames_and_readability_guards():
    from app.modules.m02_content import ContentOptimization, flesch
    from app.modules.m05_images import non_descriptive_filename
    s = site([page("/", text="Welcome to the shop. " * 30), page("/sale", text="Lorem ipsum dolor sit amet, consectetur.")])
    r = run(ContentOptimization(), ctx(s))["content.placeholder"]
    assert [i.url for i in r.items] == [BASE + "/sale"]
    assert not non_descriptive_filename("A11559_25Q3_Cruiser_Dark_Navy_Blizzard_PDP_LEFT-2000x2000_9f5ba943.png")
    assert non_descriptive_filename("683DB48C-C4E4-45DF-BA8A-928574FB89EB-1024x768.jpeg")
    assert non_descriptive_filename("IMG_3757-1024x768.jpeg")
    menu = " ".join(["Shop Men Shop Women New Arrivals Bestsellers Sale"] * 40)  # no sentences: not scored
    assert flesch(menu) is None


def test_bot_challenge_page_is_not_content():
    from app.crawler.crawl import CHALLENGE
    assert CHALLENGE.search("<html><head><title>Just a moment...</title></head>")
    assert CHALLENGE.search('<script src="/cdn-cgi/challenge-platform/h/b/orchestrate/x"></script>')
    assert not CHALLENGE.search("<title>Just a moment with our founder</title>")
    s = site([page("/", links=[link("/survey")]), page("/survey", error="Bot-protection challenge page instead of content")])
    r = run(TechnicalAudit(), ctx(s))
    assert BASE + "/survey" in [i.url for i in r["tech.fetch_errors"].items]
    assert BASE + "/survey" not in {p.url for p in s.html_pages()}


def test_collection_page_is_not_a_product():
    from app.modules.m09_schema import SchemaMarkup
    coll = page("/collections/all", text="Recycling bag $0.00 Add to cart Boxes $0.00 Add to cart")
    prod = page("/products/bag", text="Recycling bag $0.00 Add to cart")
    r = run(SchemaMarkup(), ctx(site([page("/"), coll, prod])))["schema.missing_types"]
    products = {i.url for i in r.items if i.evidence["missing_type"] == "Product"}
    assert BASE + "/products/bag" in products and BASE + "/collections/all" not in products


def test_webflow_pagination_and_first_page():
    from app.crawler.urls import is_first_page_param, is_pagination
    assert is_pagination("https://x.com/?d06e130e_page=2") and not is_pagination("https://x.com/?d06e130e_page=1")
    assert is_first_page_param("https://x.com/?d06e130e_page=1") and not is_first_page_param("https://x.com/")


def test_listing_pages_are_judged_once():
    from app.sitemodel.types import PageData, SiteFacts, SiteModel
    site = SiteModel(facts=SiteFacts(input_url="https://x.com/", host="x.com", origin="https://x.com", start_url="https://x.com/"))
    for u in ("https://x.com/", "https://x.com/?d06e130e_page=1", "https://x.com/?d06e130e_page=2", "https://x.com/about"):
        site.pages[u] = PageData(url=u, final_url=u, status=200, content_type="text/html", is_html=True, depth=0)
    assert all(p.indexable for p in site.pages.values())
    assert sorted(p.url for p in site.content_pages()) == ["https://x.com/", "https://x.com/about"]


def test_http_page_that_only_fails_over_http_is_not_broken():
    from app.crawler.crawl import Crawler, HTTP_ONLY_UNREACHABLE
    from app.crawler.urls import SiteScope

    class FakeFetcher:
        async def fetch(self, url, **kw):
            return FetchResult(url=url, final_url=url, status=None, error="ConnectError: [Errno -2] Name or service not known")

        async def check(self, url, **kw):
            return FetchResult(url=url, final_url=url, status=200 if url.startswith("https://") else None)

    c = Crawler.__new__(Crawler)
    c.scope, c.fetcher = SiteScope("ex.com"), FakeFetcher()
    c.pages = {"http://ex.com/canvas": page("/canvas", url="http://ex.com/canvas", status=None,
                                            error="ConnectError: [Errno -2] Name or service not known")}
    c.url_status = {}
    asyncio.run(c._verify_errors(pause_s=0))
    assert "http://ex.com/canvas" not in c.pages and c.url_status["http://ex.com/canvas"].error == HTTP_ONLY_UNREACHABLE
    s = site([page("/", links=[link("http://ex.com/canvas", internal=True)]), page("/canvas")],
             url_status={"http://ex.com/canvas": c.url_status["http://ex.com/canvas"]})
    r = run(TechnicalAudit(), ctx(s))
    assert r["tech.broken_internal_links"].status.value == "PASS"


def test_viewport_initial_scale_only_is_responsive():
    from app.modules.m06_technical import _responsive_viewport
    assert _responsive_viewport("initial-scale=1")
    assert _responsive_viewport("width=device-width, initial-scale=1")
    assert not _responsive_viewport("width=1024, initial-scale=1")
    assert not _responsive_viewport("user-scalable=no")


def test_shopify_width_template_images_are_resolved():
    from app.crawler.parser import parse_page
    from app.crawler.urls import SiteScope
    from app.sitemodel.types import PageData
    html = ('<html><body><img class="lazyload" src="data:image/gif;base64,R0" '
            'data-src="//shop.example/cdn/shop/files/fabric_{width}x.png?v=1" data-widths="[180,360,540,720,900]" alt="x">'
            '<img src="/cdn/shop/files/roll_%7Bwidth%7Dx.jpg"></body></html>')
    page = PageData(url="https://shop.example/", final_url="https://shop.example/")
    parse_page(page, html, SiteScope("shop.example"))
    srcs = [i.src for i in page.images]
    assert "https://shop.example/cdn/shop/files/fabric_720x.png?v=1" in srcs
    assert "https://shop.example/cdn/shop/files/roll_800x.jpg" in srcs


def test_about_page_variants():
    from app.modules.m17_geo import EEAT_PATHS
    assert EEAT_PATHS["about"].search("https://x.com/pages/about-california-textile-group")
    assert not EEAT_PATHS["about"].search("https://x.com/pages/aboutness")


def test_repeated_card_text_does_not_make_listings_near_duplicates():
    from app.crawler.simhash import hamming, simhash
    card = " Regular price $0.00 Sale price $0.00 Regular price Unit price per Sold out "
    a = card.join(f"COTTON JERSEY {w} GSM" for w in range(100, 140)) 
    b = card.join(f"MODAL RIB {w} GSM TRIBLEND" for w in range(300, 340))
    from app.config import env  # noqa: F401
    from app.sitemodel import builder
    assert hamming(simhash(a), simhash(b)) > builder.NEAR_DUPLICATE_HAMMING
    assert simhash("same text here twice") == simhash("same text here twice")


def test_facet_and_scoped_product_urls():
    from app.crawler.urls import is_facet_url, is_scoped_product_url
    assert is_facet_url("https://s.com/collections/mens?sort_by=manual")
    assert is_facet_url("https://s.com/collections/mens?filter.v.price.gte=10&sort_by=price")
    assert not is_facet_url("https://s.com/collections/mens?page=2")
    assert not is_facet_url("https://s.com/collections/mens")
    assert is_scoped_product_url("https://s.com/collections/mens/products/westsider")
    assert not is_scoped_product_url("https://s.com/products/westsider")


def test_domain_like_link_is_a_page_not_a_file():
    from app.modules.common import resource_kind
    assert resource_kind("https://s.com/blogs/posts/comunitymade.com") == "page"
    assert resource_kind("https://s.com/file.pdf") == "PDF file"


def test_shopify_section_group_chrome_is_not_content():
    from app.crawler.parser import parse_page
    from app.crawler.urls import SiteScope
    from app.sitemodel.types import PageData
    menu = " ".join(["SHOP MEN Low Tops Mid Tops Slip-Ons"] * 20)
    html = (f'<html><body><div id="shopify-section-sections--1__announcement-bar">FREE USA SHIPPING ON $100+</div>'
            f'<div id="shopify-section-sections--1__header">{menu}</div>'
            '<div id="shopify-section-template--9__main"><h1>Westsider</h1><p>Handmade in Los Angeles from Italian leather.</p></div>'
            '<div id="shopify-section-sections--2__footer-main">Newsletter signup and links</div></body></html>')
    p = PageData(url="https://s.com/products/w", final_url="https://s.com/products/w")
    parse_page(p, html, SiteScope("s.com"))
    assert "Italian leather" in p.text and "SHOP MEN" not in p.text and "Newsletter" not in p.text


def test_jsonld_graph_items_inherit_context():
    from app.crawler.parser import _jsonld_walk
    out = []
    _jsonld_walk({"@context": "https://schema.org", "@graph": [{"@type": "WebPage", "name": "x"},
                                                              {"@type": "Organization", "name": "y"}]}, out)
    assert all(i["@context"] == "https://schema.org" for i in out) and len(out) == 2


def test_cloudflare_email_protection_is_not_a_page():
    from app.crawler.urls import is_action_url
    assert is_action_url("https://x.com/cdn-cgi/l/email-protection")
    assert not is_action_url("https://x.com/about/cdn-cgi-guide")


def test_wordpress_pages_with_og_article_are_not_articles():
    from app.modules.m09_schema import is_article
    from app.sitemodel.types import PageData
    about = PageData(url="https://x.com/about/", final_url="https://x.com/about/")
    about.og = {"type": "article"}
    about.dates = {"modified_time": "2026-10-22", "datePublished": "2024-01-01"}  # Yoast WebPage node
    about.structured_data = {"types": ["WebPage", "BreadcrumbList", "Organization"]}
    post = PageData(url="https://x.com/lawsuit-update/", final_url="https://x.com/lawsuit-update/")
    post.og = {"type": "article"}
    post.dates = {"published_time": "2026-10-20", "modified_time": "2026-10-20"}
    assert not is_article(about) and is_article(post)


def test_scoped_product_urls_are_crawled_after_sitemap_pages():
    import itertools
    from types import SimpleNamespace
    from app.crawler.crawl import Crawler
    from app.crawler.urls import SiteScope
    B = "https://s.com"
    links = {B + "/": [B + "/collections/a/products/x", B + "/about", B + "/collections/a/products/y"],
             B + "/about": [], B + "/products/x": []}
    c = Crawler.__new__(Crawler)
    c.cfg = SimpleNamespace(max_pages=4, concurrency=1)
    c.fetched, c.dropped_for_limit, c._seq, c._deferred = 0, 0, itertools.count(), []
    c.seen, c.scope, c.order = {B + "/"}, SiteScope("s.com"), []
    c.robots = SimpleNamespace(can_fetch=lambda u, a: True)
    c.ua_token = "x"

    class F:
        async def fetch(self, url, **kw):
            return url
    c.fetcher = F()

    async def noop(*a, **k):
        return None
    c._check_cancel, c._report = noop, noop

    async def ingest(url, res, depth, via, queue, prio=0):
        c.order.append(url)
        for t in links.get(url, []):
            if t not in c.seen:
                c.seen.add(t)
                c._enqueue_link(queue, t, 1, prio + 1)
    c._ingest = ingest

    async def go():
        q = asyncio.PriorityQueue()
        q.put_nowait((0, next(c._seq), B + "/", 0, "start"))
        await c._crawl_queue(q, {B + "/products/x": None, B + "/products/z": None})
    asyncio.run(go())
    # links first, then sitemap pages, then (budget permitting) the duplicate product addresses
    assert c.order[:4] == [B + "/", B + "/about", B + "/products/x", B + "/products/z"]
    assert c.fetched == 4
    c.cfg.max_pages, c.fetched, c.order, c.seen, c._deferred = 10, 0, [], {B + "/"}, []
    asyncio.run(go())
    assert c.order[4:] == [B + "/collections/a/products/x", B + "/collections/a/products/y"]


def test_scoped_product_links_count_for_the_product_page():
    from app.modules.m04_internal_links import InternalLinking
    s = site([page("/", links=[link("/collections/mens/products/westsider")]),
              page("/collections/mens/products/westsider", canonical=BASE + "/products/westsider"),
              page("/products/westsider")],
             sitemap={BASE + "/products/westsider": None})
    assert s.pages[BASE + "/products/westsider"].inlinks >= 1
    r = run(InternalLinking(), ctx(s))
    assert r["links.orphans"].status.value == "PASS"
