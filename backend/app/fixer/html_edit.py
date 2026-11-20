"""Precise edits on source text where the tags appear literally (HTML, Liquid, Astro, JSX, templates…).

Every function returns the new text, or None when it can't make the change safely (the value is computed by
template code, the tag isn't there, the file has no </head>, …). None sends the change to the AI editor or to
the manual list; we never guess.
"""
from __future__ import annotations

import html
import re
from urllib.parse import urlsplit

TEMPLATE_EXPR = re.compile(r"\{\{|\{%|<%|<\?|\{[A-Za-z_$][\w$.]*\}|\$\{")
_TITLE = re.compile(r"(<title\b[^>]*>)(.*?)(</title>)", re.I | re.S)
_HEAD_CLOSE = re.compile(r"</head\s*>", re.I)
_TAG = r"<{name}\b[^>]*>"


def esc(v: str) -> str:
    return html.escape(v, quote=True)


def _literal(s: str) -> bool:
    return not TEMPLATE_EXPR.search(s)


def _attr(tag: str, name: str) -> str | None:
    m = re.search(rf"""\s{name}\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""", tag, re.I)
    return None if not m else next(g for g in m.groups() if g is not None)


def _set_attr(tag: str, name: str, value: str) -> str:
    pat = re.compile(rf"""(\s{name}\s*=\s*)(?:"[^"]*"|'[^']*'|[^\s>]+)""", re.I)
    if pat.search(tag):
        return pat.sub(lambda m: f'{m.group(1)}"{esc(value)}"', tag, count=1)
    end = "/>" if tag.rstrip().endswith("/>") else ">"
    body = tag.rstrip()[: -len(end)].rstrip()
    return f'{body} {name}="{esc(value)}"{" /" if end == "/>" else ""}>'


def _insert_in_head(text: str, snippet: str) -> str | None:
    """Insert before </head>, on its own lines with matching indentation when </head> is on its own line."""
    m = _HEAD_CLOSE.search(text)
    if not m:
        return None
    line_start = text.rfind("\n", 0, m.start()) + 1
    before = text[line_start:m.start()]
    if before.strip() == "":
        indent = before + "  "
        block = "".join(f"{indent}{line}\n" for line in snippet.strip("\n").splitlines())
        return text[:line_start] + block + text[line_start:]
    return text[:m.start()] + snippet.strip() + text[m.start():]


# files where tags can be edited as text; JSX/Vue/Svelte/Markdown go to the AI editor instead
DIRECT_EXT = {"html", "htm", "liquid", "njk", "hbs", "ejs", "erb", "php", "twig", "astro"}


def direct_editable(path: str) -> bool:
    return path.rsplit(".", 1)[-1].lower() in DIRECT_EXT


def set_title(text: str, new: str) -> str | None:
    m = _TITLE.search(text)
    if m:
        if not _literal(m.group(2)):
            return None
        return text[:m.start()] + m.group(1) + html.escape(new, quote=False) + m.group(3) + text[m.end():]
    return _insert_in_head(text, f"<title>{html.escape(new, quote=False)}</title>")


def _find_meta(text: str, key: str, value: str) -> re.Match | None:
    for m in re.finditer(_TAG.format(name="meta"), text, re.I):
        if (_attr(m.group(0), key) or "").lower() == value.lower():
            return m
    return None


def set_meta(text: str, key: str, value: str, content: str) -> str | None:
    """<meta name="description" …>, <meta property="og:title" …>: replace content or add the tag in <head>."""
    m = _find_meta(text, key, value)
    if m:
        old = _attr(m.group(0), "content") or ""
        if not _literal(old) or not _literal(m.group(0)):
            return None
        return text[:m.start()] + _set_attr(m.group(0), "content", content) + text[m.end():]
    return _insert_in_head(text, f'<meta {key}="{value}" content="{esc(content)}">')


def set_meta_description(text: str, desc: str) -> str | None:
    return set_meta(text, "name", "description", desc)


def ensure_link(text: str, rel: str, href: str) -> str | None:
    for m in re.finditer(_TAG.format(name="link"), text, re.I):
        if (_attr(m.group(0), "rel") or "").lower() == rel:
            return text  # already present: leave it alone (the audit reports conflicts separately)
    return _insert_in_head(text, f'<link rel="{rel}" href="{esc(href)}">')


def set_html_lang(text: str, lang: str) -> str | None:
    m = re.search(_TAG.format(name="html"), text, re.I)
    if not m:
        return None
    if _attr(m.group(0), "lang"):
        return text
    return text[:m.start()] + _set_attr(m.group(0), "lang", lang) + text[m.end():]


def ensure_viewport(text: str) -> str | None:
    if _find_meta(text, "name", "viewport"):
        return set_meta(text, "name", "viewport", "width=device-width, initial-scale=1")
    return _insert_in_head(text, '<meta name="viewport" content="width=device-width, initial-scale=1">')


def insert_jsonld(text: str, code: str) -> str | None:
    """Add a <script type="application/ld+json"> block (code may already include the script tag)."""
    code = code.strip()
    if not code.lower().startswith("<script"):
        code = f'<script type="application/ld+json">\n{code}\n</script>'
    body = re.sub(r"\s+", "", re.sub(r"</?script[^>]*>", "", code))
    if body and body in re.sub(r"\s+", "", text):
        return text  # identical block already there
    return _insert_in_head(text, code)


def _img_key(src: str) -> str:
    p = urlsplit(src.strip())
    path = p.path
    return re.sub(r"_\d+x\d*(?=\.\w+$)", "", path.rsplit("/", 1)[-1]).lower() if path else ""


def _same_image(cand: str, src: str, full: str, key: str) -> bool:
    """Same file: same path, or same file name (ignoring Shopify-style _800x size suffixes) on the same host
    or in a relative src. A same-named file on another server is a different image."""
    c = urlsplit(cand.strip())
    if c.path == full and (not c.netloc or c.netloc == urlsplit(src).netloc):
        return True
    return _img_key(cand) == key and (not c.netloc or c.netloc == urlsplit(src).netloc)


def set_img_attrs(text: str, src: str, *, alt: str | None = None, width: int | None = None,
                  height: int | None = None) -> tuple[str, int]:
    """Set alt / add missing width+height on every <img> whose src (or data-src/srcset) is this image file.
    Existing non-empty alts and existing sizes are kept. Returns (new_text, number_of_tags_changed)."""
    key = _img_key(src)
    full = urlsplit(src).path
    if not key:
        return text, 0
    out, last, n = [], 0, 0
    for m in re.finditer(_TAG.format(name="img"), text, re.I):
        tag = m.group(0)
        cands = [_attr(tag, a) or "" for a in ("src", "data-src")] + [s.split()[0] for s in (_attr(tag, "srcset") or "").split(",") if s.strip()]
        if not any(c and _same_image(c, src, full, key) for c in cands):
            continue
        new = tag
        if alt is not None and not _attr(tag, "alt") and _literal(tag):
            new = _set_attr(new, "alt", alt)
        if width and height and _attr(tag, "width") is None and _attr(tag, "height") is None:
            new = _set_attr(_set_attr(new, "width", str(width)), "height", str(height))
        if new != tag:
            out.append(text[last:m.start()] + new)
            last, n = m.end(), n + 1
    if not n:
        return text, 0
    out.append(text[last:])
    return "".join(out), n


def append_robots_sitemap(text: str | None, sitemap_url: str) -> str:
    if text and re.search(r"^\s*sitemap\s*:", text, re.I | re.M):
        return text
    base = text if text and text.strip() else "User-agent: *\nAllow: /\n"
    return base.rstrip("\n") + f"\n\nSitemap: {sitemap_url}\n"


def sitemap_xml(urls: list[str]) -> str:
    rows = "".join(f"  <url><loc>{html.escape(u, quote=False)}</loc></url>\n" for u in urls)
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n' + rows + "</urlset>\n")


def _indent_block(snippet: str, indent: str) -> str:
    return "".join(f"{indent}{line}\n" if line.strip() else "\n" for line in snippet.strip("\n").splitlines())


def insert_main_end(text: str, snippet: str) -> str | None:
    """Add content at the end of the page's main content: before the last </main>, else before the first
    top-level <footer>, else before </body>."""
    for rx in (r"</main\s*>", None):
        if rx:
            ms = list(re.finditer(rx, text, re.I))
            if ms:
                m = ms[-1]
                break
        else:
            m = re.search(r"\n([ \t]*)<footer\b", text, re.I) or re.search(r"</body\s*>", text, re.I)
    if not m:
        return None
    start = text.rfind("\n", 0, m.start()) + 1
    before = text[start:m.start()]
    if before.strip() == "":
        return text[:start] + _indent_block(snippet, before + "  ") + text[start:]
    return text[:m.start()] + "\n" + snippet.strip() + "\n" + text[m.start():]


def insert_after_h1(text: str, snippet: str) -> str | None:
    """Add content right after the first <h1>…</h1> (e.g. an answer-first opening paragraph)."""
    m = re.search(r"<h1\b[^>]*>.*?</h1\s*>", text, re.I | re.S)
    if not m:
        return None
    line_start = text.rfind("\n", 0, m.start()) + 1
    indent = re.match(r"[ \t]*", text[line_start:]).group(0)
    return text[:m.end()] + "\n" + _indent_block(snippet, indent).rstrip("\n") + text[m.end():]
