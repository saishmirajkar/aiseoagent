from app.crawler.robots import build_robots, parse_groups
from app.crawler.simhash import content_hash, hamming, simhash
from app.crawler.urls import SiteScope, ensure_scheme, looks_like_page, normalize_url, template_key
from app.modules.pixel_width import title_px


# ------------------------------------------------------------------ URLs
def test_normalize_basics():
    assert normalize_url("HTTPS://Example.COM:443/a/../b/./c?x=1#frag") == "https://example.com/b/c?x=1"
    assert normalize_url("http://example.com") == "http://example.com/"
    assert normalize_url("http://example.com:8080/x/") == "http://example.com:8080/x/"
    assert normalize_url("/rel", "https://ex.com/dir/page") == "https://ex.com/rel"
    assert normalize_url("rel2", "https://ex.com/dir/page") == "https://ex.com/dir/rel2"
    assert normalize_url("mailto:a@b.c") is None
    assert normalize_url("javascript:void(0)") is None
    assert normalize_url("https://ex.com/caf%C3%A9 x") == "https://ex.com/caf%C3%A9%20x"
    assert normalize_url("https://not a host/") is None
    assert normalize_url("https://bücher.de/") == "https://xn--bcher-kva.de/"
    assert normalize_url("http://[::1]:8080/x") == "http://[::1]:8080/x"


def test_ensure_scheme_and_scope():
    assert ensure_scheme("example.com") == "https://example.com"
    s = SiteScope("www.ex.com")
    assert s.is_internal("https://ex.com/a") and s.is_internal("http://www.ex.com/")
    assert not s.is_internal("https://blog.ex.com/")
    assert SiteScope("ex.com", include_subdomains=True).is_internal("https://blog.ex.com/")


def test_template_key_and_page_types():
    assert template_key("https://ex.com/") == "/"
    assert template_key("https://ex.com/blog/my-post") == "/blog/*"
    assert template_key("https://ex.com/p/1/red") == "/p/*/*"
    assert looks_like_page("https://ex.com/a.html") and not looks_like_page("https://ex.com/a.pdf")


# ------------------------------------------------------------------ robots.txt
ROBOTS = """User-agent: *
Disallow: /private/
Allow: /private/ok
Crawl-delay: 2

User-agent: GPTBot
User-agent: CCBot
Disallow: /

Sitemap: https://ex.com/sitemap.xml
"""


def test_robots_groups_and_matching():
    groups, sitemaps = parse_groups(ROBOTS)
    assert sitemaps == ["https://ex.com/sitemap.xml"]
    assert groups[1].agents == ["GPTBot", "CCBot"]
    r = build_robots("https://ex.com/robots.txt", 200, ROBOTS)
    assert not r.can_fetch("https://ex.com/private/x", "Googlebot")
    assert r.can_fetch("https://ex.com/private/ok", "Googlebot")
    assert not r.can_fetch("https://ex.com/", "GPTBot")
    assert r.crawl_delay("SEOInspector") == 2.0
    ai = r.ai_bot_rules("https://ex.com")
    assert ai["GPTBot"]["root_allowed"] is False and ai["GPTBot"]["has_specific_group"]
    assert ai["ClaudeBot"]["root_allowed"] is True and not ai["ClaudeBot"]["has_specific_group"]


def test_robots_status_semantics():
    assert build_robots("u", 404, "").can_fetch("https://ex.com/x", "Googlebot")  # 4xx = allow all
    r5 = build_robots("u", 503, "")
    assert r5.unreachable and not r5.can_fetch("https://ex.com/x", "Googlebot")  # 5xx = disallow all
    assert build_robots("u", None, "", "timeout").unreachable


# ------------------------------------------------------------------ simhash / content hash
def test_simhash_near_duplicates():
    a = "Acme widgets are durable and precise tools used in workshops and factories every day " * 5
    b = a + " Extra sentence."
    c = "Completely different content about gardening, roses, soil, watering schedules and pests " * 5
    assert content_hash(a) == content_hash(a.upper())
    assert hamming(simhash(a), simhash(b)) <= 3
    assert hamming(simhash(a), simhash(c)) > 10
    v = simhash(c)
    assert -(2 ** 63) <= v < 2 ** 63  # fits Postgres BIGINT


# ------------------------------------------------------------------ title pixel width
def test_title_pixel_width():
    assert title_px("iiii") < title_px("WWWW")
    assert 250 < title_px("A perfectly normal title for a page") < 380
    assert title_px("x" * 70) > 580
