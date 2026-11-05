"""Fetcher, sitemap and image probing against an in-memory HTTP transport."""
import asyncio
import gzip
import struct

import httpx

from app.crawler.fetcher import Fetcher, decode_body
from app.crawler.imageinfo import probe_image, sniff
from app.crawler.rate_limit import HostRateLimiter
from app.crawler.sitemap import collect_sitemaps


def make_fetcher(handler, **kw) -> Fetcher:
    f = Fetcher("TestBot/1.0", HostRateLimiter(1000), timeout=5, retries=kw.pop("retries", 1), **kw)
    f._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)
    return f


def run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------------ fetcher
def test_redirect_chain_and_final():
    def h(req):
        p = req.url.path
        if p == "/a":
            return httpx.Response(301, headers={"location": "/b"})
        if p == "/b":
            return httpx.Response(302, headers={"location": "https://ex.com/c"})
        return httpx.Response(200, html="<html>ok</html>")
    r = run(make_fetcher(h).fetch("https://ex.com/a"))
    assert [x.status for x in r.chain] == [301, 302]
    assert r.final_url == "https://ex.com/c" and r.status == 200 and r.is_html


def test_redirect_loop_detected():
    def h(req):
        return httpx.Response(301, headers={"location": "/y" if req.url.path == "/x" else "/x"})
    r = run(make_fetcher(h).fetch("https://ex.com/x"))
    assert r.redirect_loop and len(r.chain) == 2


def test_retry_on_503_then_success():
    calls = {"n": 0}

    def h(req):
        calls["n"] += 1
        return httpx.Response(503, headers={"retry-after": "0"}) if calls["n"] == 1 else httpx.Response(200, text="ok")
    r = run(make_fetcher(h).fetch("https://ex.com/"))
    assert r.status == 200 and calls["n"] == 2


def test_body_cap_and_html_only():
    def h(req):
        if req.url.path == "/big":
            return httpx.Response(200, content=b"x" * 50_000, headers={"content-type": "text/html"})
        return httpx.Response(200, content=b"%PDF" * 100, headers={"content-type": "application/pdf"})
    f = make_fetcher(h, max_bytes=1000)
    big = run(f.fetch("https://ex.com/big"))
    assert big.truncated and len(big.body) == 1000
    pdf = run(f.fetch("https://ex.com/doc.pdf", html_only=True))
    assert pdf.body == b"" and pdf.status == 200


def test_robots_blocked_redirect():
    def h(req):
        return httpx.Response(301, headers={"location": "/private/x"})
    f = make_fetcher(h, allow=lambda u: "/private/" not in u)
    r = run(f.fetch("https://ex.com/a"))
    assert r.blocked_redirect


def test_decode_body_charsets():
    assert decode_body("café".encode("latin-1"), "text/html; charset=iso-8859-1") == "café"
    assert decode_body(b'<meta charset="utf-8">caf\xc3\xa9', "text/html") .endswith("café")
    assert decode_body(b"\xef\xbb\xbfhi", "") == "hi"


# ------------------------------------------------------------------ sitemaps
URLSET = ("<?xml version='1.0'?><urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>"
          "<url><loc>https://ex.com/a</loc><lastmod>2024-01-01</lastmod></url>"
          "<url><loc>https://ex.com/b</loc></url></urlset>")


def test_sitemap_index_and_gzip():
    def h(req):
        p = req.url.path
        if p == "/sitemap.xml":
            return httpx.Response(200, text="<sitemapindex xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>"
                                             "<sitemap><loc>https://ex.com/s1.xml.gz</loc></sitemap></sitemapindex>")
        if p == "/s1.xml.gz":
            return httpx.Response(200, content=gzip.compress(URLSET.encode()))
        return httpx.Response(404)
    res = run(collect_sitemaps(make_fetcher(h), "https://ex.com", ["https://ex.com/sitemap.xml"], 1000))
    assert res.found
    assert res.urls == {"https://ex.com/a": "2024-01-01", "https://ex.com/b": None}
    assert [f.kind for f in res.files] == ["sitemapindex", "urlset"]


def test_sitemap_fallback_and_errors():
    def h(req):
        if req.url.path == "/sitemap.xml":
            return httpx.Response(200, text="<urlset><url><loc>https://ex.com/a</loc></url>")  # unclosed
        return httpx.Response(404)
    res = run(collect_sitemaps(make_fetcher(h), "https://ex.com", [], 1000))
    assert len(res.files) == 1 and res.files[0].source == "fallback"
    assert res.files[0].error and "XML" in res.files[0].error
    assert not res.found


def test_sitemap_not_xml_and_limit():
    def h(req):
        if req.url.path == "/html":
            return httpx.Response(200, text="<html><body>hi</body></html>")
        return httpx.Response(200, text=URLSET)
    f = make_fetcher(h)
    bad = run(collect_sitemaps(f, "https://ex.com", ["https://ex.com/html"], 1000))
    assert bad.files[0].error and not bad.found
    lim = run(collect_sitemaps(f, "https://ex.com", ["https://ex.com/sm.xml"], 1))
    assert len(lim.urls) == 1 and lim.truncated


# ------------------------------------------------------------------ image headers
def _png(w, h):
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", w, h) + b"\x08\x02\x00\x00\x00"


def test_sniff_formats():
    assert (sniff(_png(640, 480)).format, sniff(_png(640, 480)).width) == ("png", 640)
    gif = b"GIF89a" + struct.pack("<HH", 10, 20)
    assert (sniff(gif).width, sniff(gif).height) == (10, 20)
    jpeg = (b"\xff\xd8\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00" + b"\x00" * 9
            + b"\xff\xc2" + struct.pack(">HBHH", 17, 8, 300, 400))
    assert (sniff(jpeg).format, sniff(jpeg).width, sniff(jpeg).height) == ("jpeg", 400, 300)
    vp8x = b"RIFF\x00\x00\x00\x00WEBPVP8X" + b"\x00" * 8 + (799).to_bytes(3, "little") + (599).to_bytes(3, "little")
    assert (sniff(vp8x).format, sniff(vp8x).width, sniff(vp8x).height) == ("webp", 800, 600)
    avif = b"\x00\x00\x00\x1cftypavif" + b"\x00" * 20 + b"ispe\x00\x00\x00\x00" + struct.pack(">II", 1200, 800)
    assert (sniff(avif).format, sniff(avif).width) == ("avif", 1200)
    assert sniff(b"<svg xmlns='http://www.w3.org/2000/svg'></svg>").format == "svg"
    assert sniff(b"garbage").format is None


def test_probe_image_streams_and_sizes():
    data = _png(1000, 500) + b"\x00" * 300_000

    def h(req):
        if req.url.path == "/404.png":
            return httpx.Response(404)
        return httpx.Response(200, content=data, headers={"content-type": "image/png",
                                                          "content-length": str(len(data))})
    f = make_fetcher(h)
    r = run(probe_image(f, "https://ex.com/big.png"))
    assert (r.format, r.width, r.height, r.bytes, r.bytes_exact) == ("png", 1000, 500, len(data), True)
    missing = run(probe_image(f, "https://ex.com/404.png"))
    assert missing.status == 404 and missing.error
