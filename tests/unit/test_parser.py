from app.crawler.parser import parse_page
from app.crawler.urls import SiteScope
from app.sitemodel.types import PageData

HTML = """<!doctype html><html lang="en"><head>
<title>Main title</title><title>Second</title>
<meta name="description" content=" A  description ">
<meta name="robots" content="noindex, follow"><meta name="googlebot" content="nosnippet">
<meta name="viewport" content="width=device-width">
<meta property="og:title" content="OG"><meta name="twitter:card" content="summary">
<link rel="canonical" href="/canon"><link rel="alternate" hreflang="en-GB" href="/en/">
<link rel="alternate" hreflang="english" href="/bad/"><link rel="stylesheet" href="http://ex.com/s.css">
<base href="https://ex.com/base/">
<script type="application/ld+json">{"@context":"https://schema.org","@graph":[{"@type":"Article","datePublished":"2024-02-01","author":{"@type":"Person","name":"Jane Doe"}},{"@type":"LocalBusiness","name":"Acme","telephone":"+1 555 000 1111"}]}</script>
</head><body>
<link rel="canonical" href="/body-canon">
<h1>One</h1><h3>Three</h3><h2>Two</h2>
<p>Hello <b>world</b>.</p><script>var hidden = "not text";</script><style>.x{}</style>
<a href="rel">Relative</a> <a href="https://other.org/" rel="nofollow sponsored">Ext</a>
<a href="https://ex.com/x"><img src="/i/logo.png" alt="Logo"></a>
<picture><source type="image/webp" srcset="/i/a.webp"><img src="/i/a.jpg" alt=""></picture>
<img data-src="/i/lazy.jpg" src="data:image/gif;base64,R0lGOD" alt="Lazy" loading="lazy" width="10" height="5">
<img src="http://ex.com/insecure.png">
<address>1 Main St, Springfield</address>
</body></html>"""


def parsed(url="https://ex.com/page", html=HTML):
    p = PageData(url=url, final_url=url)
    sig = parse_page(p, html, SiteScope("ex.com"))
    return p, sig


def test_head_fields():
    p, _ = parsed()
    assert p.title == "Main title" and p.title_count == 2
    assert p.meta_description == "A description"
    assert p.is_noindex and "googlebot: nosnippet" in p.meta_robots
    assert p.canonical == "https://ex.com/canon"  # resolved before <base>? first canonical wins
    assert p.canonical_in_body and len(p.canonicals) == 2
    assert p.lang == "en" and p.viewport == "width=device-width"
    assert p.og["title"] == "OG" and p.twitter["card"] == "summary"
    codes = {h["lang"]: h["valid_code"] for h in p.hreflang}
    assert codes == {"en-GB": True, "english": False}


def test_body_fields():
    p, sig = parsed()
    assert p.headings == [(1, "One"), (3, "Three"), (2, "Two")]
    assert "not text" not in p.text and "Hello world" in p.text
    links = {l.target: l for l in p.links}
    assert "https://ex.com/base/rel" in links  # <base href> respected
    ext = links["https://other.org/"]
    assert not ext.internal and ext.nofollow and ext.sponsored
    assert links["https://ex.com/x"].anchor == "Logo" and links["https://ex.com/x"].image_link
    imgs = {i.src.rsplit("/", 1)[-1]: i for i in p.images}
    assert imgs["a.jpg"].alt == "" and imgs["a.jpg"].modern_source
    assert imgs["lazy.jpg"].loading == "lazy" and imgs["lazy.jpg"].width_attr == 10
    assert imgs["insecure.png"].alt is None
    assert imgs["logo.png"].in_link
    assert not sig.looks_like_shell


def test_structured_data_nap_dates_authors():
    p, _ = parsed()
    assert set(p.structured_data["types"]) == {"Article", "LocalBusiness"}
    assert p.dates["datePublished"] == "2024-02-01"
    assert "Jane Doe" in p.authors
    assert p.nap["schema"]["telephone"] == "+1 555 000 1111"
    assert "Main St" in p.nap["address_element"]


def test_mixed_content_only_on_https():
    p, _ = parsed()
    assert "http://ex.com/insecure.png" in p.mixed_content and "http://ex.com/s.css" in p.mixed_content
    p2, _ = parsed("http://ex.com/page")
    assert p2.mixed_content == []


def test_jsonld_syntax_error_reported():
    p, _ = parsed(html='<html><head><script type="application/ld+json">{"a":}</script></head><body></body></html>')
    assert p.structured_data["errors"] and "syntax" in p.structured_data["errors"][0]


def test_js_shell_detection():
    _, sig = parsed(html='<html><body><div id="root"></div><script src="/app.js"></script></body></html>')
    assert sig.looks_like_shell
    _, sig2 = parsed(html="<html><body><noscript>Please enable JavaScript</noscript></body></html>")
    assert sig2.looks_like_shell


def test_image_link_text_detection():
    html = ('<html><body><a href="/a"><img src="/1.png" alt=""></a>'
            '<a href="/b"><img src="/2.png" alt="">Shop karts</a>'
            '<a href="/c" aria-label="Contact"><img src="/3.png" alt=""></a></body></html>')
    p, _ = parsed(html=html)
    flags = {i.filename: i.link_text_empty for i in p.images}
    assert flags == {"1.png": True, "2.png": False, "3.png": False}


def test_br_separates_words():
    p, _ = parsed(html='<html><body><h1>We<br><span>outperform</span><br>giants.</h1><p>a<br/>b</p></body></html>')
    assert p.headings == [(1, "We outperform giants.")] and "a b" in p.text
