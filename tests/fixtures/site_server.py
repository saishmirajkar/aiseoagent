"""A small local website with deliberate SEO problems, for the end-to-end test and demos.

    python -m tests.fixtures.site_server        # serves on http://127.0.0.1:8765
"""
from __future__ import annotations

import struct
import threading
import time
import zlib

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse, RedirectResponse, Response
from starlette.routing import Route

LOREM = ("Acme widgets are built for durability and precision. Our engineers test every widget for strength, "
         "balance and finish before it ships. Customers use them in workshops, factories and homes. ")


def png_bytes(w: int, h: int) -> bytes:
    def chunk(t: bytes, d: bytes) -> bytes:
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + b"\x80\x80\x80" * w for _ in range(h))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


_JPEG_CACHE: dict[tuple[int, int], bytes] = {}


def jpeg_bytes(w: int, h: int) -> bytes:
    """A real, decodable JPEG with noise so it is heavy (~200 KB at 2000x1500)."""
    if (w, h) not in _JPEG_CACHE:
        import io
        import random
        from PIL import Image
        rnd = random.Random(42)
        small = Image.frombytes("RGB", (w // 8, h // 8), bytes(rnd.getrandbits(8) for _ in range((w // 8) * (h // 8) * 3)))
        buf = io.BytesIO()
        small.resize((w, h)).save(buf, "JPEG", quality=70)
        _JPEG_CACHE[(w, h)] = buf.getvalue()
    return _JPEG_CACHE[(w, h)]


def page(title: str | None, body: str, *, desc: str | None = None, canonical: str | None = None,
         lang: str | None = "en", viewport: bool = True, head_extra: str = "") -> str:
    parts = ["<!doctype html>", f"<html{f' lang={lang!r}' if lang else ''}><head><meta charset='utf-8'>"]
    if title is not None:
        parts.append(f"<title>{title}</title>")
    if desc:
        parts.append(f"<meta name='description' content='{desc}'>")
    if viewport:
        parts.append("<meta name='viewport' content='width=device-width, initial-scale=1'>")
    if canonical:
        parts.append(f"<link rel='canonical' href='{canonical}'>")
    parts.append(head_extra + "</head><body>" + body + "</body></html>")
    return "".join(parts)


def build_app(ext_base: str = "http://localhost:8766") -> Starlette:
    def base(r: Request) -> str:
        return str(r.base_url).rstrip("/")

    home_desc = "Acme Widgets makes durable, precise widgets for workshops, factories and homes. Browse products, read guides and get support."

    async def home(r: Request):
        b = base(r)
        body = f"""
        <img src="/img/hero.png" alt="Acme workshop" loading="lazy" width="300" height="200">
        <h1>Acme Widgets</h1><p>{LOREM * 3}</p>
        <nav><a href="/about">About us</a> <a href="/team">Team</a> <a href="/products">Products</a>
        <a href="/blog/post-1">Blog</a> <a href="/old-page">Old page</a> <a href="/missing">Missing</a>
        <a href="/noindex-page">Hidden</a> <a href="/js-page">App</a> <a href="/loop-a">Loop</a>
        <a href="/private/report">Report</a> <a href="mailto:hi@example.com">Mail</a></nav>
        <a href="{ext_base}/ok">Partner</a> <a href="{ext_base}/dead">Dead partner</a>
        <img src="/img/IMG_1234.jpg" width="400">
        <img src="/img/logo.png" alt="Acme logo" width="120" height="80">
        <a href="/products"><img src="/img/logo.png" alt=""></a>
        <script src="/assets/blocked.js"></script>"""
        head = (f"<link rel='alternate' hreflang='en' href='{b}/'><link rel='alternate' hreflang='de' href='{b}/de/'>"
                f"<meta property='og:title' content='Acme'><meta property='og:description' content='x'>"
                f"<meta property='og:image' content='{b}/img/logo.png'>")
        return Response(page("Acme Widgets – Durable Widgets for Every Workshop", body, desc=home_desc,
                             canonical=f"{b}/", head_extra=head), media_type="text/html")

    async def about(r: Request):
        return Response(page("About Acme Widgets | Acme", f"<h1>About</h1><p>{LOREM * 2}</p><a href='/'>Home</a>",
                             canonical=f"{base(r)}/about", viewport=False), media_type="text/html")

    async def team(r: Request):
        return Response(page("About Acme Widgets | Acme", f"<h1>Team</h1><p>Our team. {LOREM}</p><a href='/'>Home</a>",
                             desc="Meet the Acme Widgets team of engineers and designers who build durable widgets for your workshop.",
                             canonical=f"{base(r)}/missing"), media_type="text/html")

    async def products(r: Request):
        return Response(page("Products – Acme Widgets catalogue and prices", f"<h1>Products</h1><p>{LOREM * 2}</p>"
                             "<a href='/'>Home</a><a href='/about'>About</a>", desc=home_desc,
                             canonical=f"{base(r)}/products"), media_type="text/html")

    async def post1(r: Request):
        t = ("The Complete and Extremely Detailed Guide to Choosing, Installing and Maintaining Industrial "
             "Widgets in 2026")
        body = (f"<h1>Guide</h1><p>{LOREM * 4}</p><a href='/'>Home</a><a href='/about'>Read more</a>"
                "<img src='/img/photo.png' alt='Widget on a bench'><img src='/img/a.png' alt='a'>"
                "<img src='/img/b.png' alt='b'><img src='/img/c.png' alt='c'>")
        return Response(page(t, body, desc="Short.", canonical=f"{base(r)}/blog/post-1", lang=None),
                        media_type="text/html")

    async def noindex(r: Request):
        return Response(page("Hidden page for testing", f"<h1>Hidden</h1><p>{LOREM}</p>",
                             head_extra="<meta name='robots' content='noindex, follow'>"), media_type="text/html")

    async def orphan(r: Request):
        return Response(page("About Acme Widgets | Acme", f"<h1>Orphan</h1><p>{LOREM}</p>",
                             desc="An orphan page that is listed in the sitemap but not linked from anywhere on the site."),
                        media_type="text/html")

    async def js_page(r: Request):
        return Response(page("Widget configurator", "<div id='root'></div><script src='/assets/app.js'></script>",
                             desc="Configure your widget online with the Acme configurator. Pick size, finish and options, then order."),
                        media_type="text/html")

    async def app_js(r: Request):
        words = (LOREM * 3).replace("'", "")
        js = ("document.getElementById('root').innerHTML = '<h1>Configurator</h1><p>" + words +
              "</p><a href=\"/js-only-target\">Configured widgets</a>';")
        return Response(js, media_type="application/javascript")

    async def js_target(r: Request):
        return Response(page("Configured widgets gallery – Acme Widgets", f"<h1>Gallery</h1><p>{LOREM}</p>",
                             desc="A gallery of widgets configured by Acme customers, with sizes, finishes and options."),
                        media_type="text/html")

    async def new_page(r: Request):
        return Response(page("New page after redirects – Acme Widgets", f"<h1>New</h1><p>{LOREM}</p>",
                             desc="This page is the final destination of a redirect chain used in the SEO Inspector test site."),
                        media_type="text/html")

    async def private(r: Request):
        return Response(page("Private", "<p>secret</p>"), media_type="text/html")

    async def robots(r: Request):
        return PlainTextResponse("User-agent: *\nDisallow: /private/\nDisallow: /assets/blocked.js\n\n"
                                 "User-agent: GPTBot\nDisallow: /\n\n"
                                 f"Sitemap: {base(r)}/sitemap.xml\n")

    async def sitemap_index(r: Request):
        return Response(f"<?xml version='1.0'?><sitemapindex xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>"
                        f"<sitemap><loc>{base(r)}/sitemap-pages.xml</loc></sitemap></sitemapindex>",
                        media_type="application/xml")

    async def sitemap_pages(r: Request):
        b = base(r)
        locs = ["/", "/about", "/products", "/gone", "/noindex-page", "/orphan", "/old-page"]
        xml = "".join(f"<url><loc>{b}{u}</loc></url>" for u in locs)
        return Response(f"<?xml version='1.0'?><urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>{xml}</urlset>",
                        media_type="application/xml")

    async def image(r: Request):
        name = r.path_params["name"]
        if name == "IMG_1234.jpg":
            return Response(jpeg_bytes(2000, 1500), media_type="image/jpeg")
        sizes = {"hero.png": (300, 200), "logo.png": (120, 80), "photo.png": (640, 480)}
        w, h = sizes.get(name, (40, 40))
        return Response(png_bytes(w, h), media_type="image/png")

    async def blocked_js(r: Request):
        return Response("console.log(1)", media_type="application/javascript")

    routes = [
        Route("/", home), Route("/about", about), Route("/team", team), Route("/products", products),
        Route("/blog/post-1", post1), Route("/noindex-page", noindex), Route("/orphan", orphan),
        Route("/js-page", js_page), Route("/assets/app.js", app_js), Route("/js-only-target", js_target),
        Route("/new-page", new_page), Route("/private/report", private), Route("/robots.txt", robots),
        Route("/sitemap.xml", sitemap_index), Route("/sitemap-pages.xml", sitemap_pages),
        Route("/img/{name}", image), Route("/assets/blocked.js", blocked_js),
        Route("/old-page", lambda r: RedirectResponse("/old-page-2", 301)),
        Route("/old-page-2", lambda r: RedirectResponse("/new-page", 301)),
        Route("/loop-a", lambda r: RedirectResponse("/loop-b", 301)),
        Route("/loop-b", lambda r: RedirectResponse("/loop-a", 301)),
    ]
    return Starlette(routes=routes)


def build_external_app() -> Starlette:
    async def ok(r: Request):
        return PlainTextResponse("ok")

    async def dead(r: Request):
        return PlainTextResponse("gone", status_code=404)
    return Starlette(routes=[Route("/ok", ok), Route("/dead", dead)])


class ServerThread:
    def __init__(self, app, host: str, port: int):
        self.server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="warning"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self):
        self.thread.start()
        for _ in range(100):
            if self.server.started:
                return self
            time.sleep(0.05)
        raise RuntimeError("fixture server did not start")

    def __exit__(self, *exc):
        self.server.should_exit = True
        self.thread.join(timeout=5)


if __name__ == "__main__":
    with ServerThread(build_external_app(), "127.0.0.1", 8766):
        uvicorn.run(build_app("http://localhost:8766"), host="127.0.0.1", port=8765)
