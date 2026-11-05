"""HTML -> Site Model fields (selectolax/lexbor for speed, extruct for microdata/RDFa)."""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

from selectolax.lexbor import LexborHTMLParser, LexborNode

from app.crawler.simhash import content_hash, simhash, tokens
from app.crawler.urls import SiteScope, filename_of, normalize_url
from app.sitemodel.types import ImageRec, LinkRec, PageData

log = logging.getLogger(__name__)

STRIP_FOR_TEXT = ["script", "style", "noscript", "template", "svg", "iframe", "object", "canvas"]
MOUNT_IDS = ("root", "app", "__next", "__nuxt", "___gatsby", "svelte", "main-app")
_WS = re.compile(r"\s+")
_PHONE = re.compile(r"(?<![\w/])(\+?\(?\d[\d\s().\-]{7,18}\d)(?![\w/])")
_HREFLANG = re.compile(r"^(x-default|[a-z]{2,3}(-[a-z]{4})?(-([a-z]{2}|\d{3}))?)$", re.I)


@dataclass
class ShellSignals:
    word_count: int
    script_count: int
    mount_empty: bool
    noscript_js: bool

    @property
    def looks_like_shell(self) -> bool:
        if self.word_count >= 80:
            return False
        return self.mount_empty or self.noscript_js or (self.script_count >= 3 and self.word_count < 30)


def _clean(s: str | None) -> str:
    return _WS.sub(" ", s or "").strip()


def _attr(node: LexborNode, name: str) -> str | None:
    v = node.attributes.get(name)
    return v if v is not None else (None if name not in node.attributes else "")


def _int(v: str | None) -> int | None:
    if not v:
        return None
    m = re.match(r"\s*(\d+)", v)
    return int(m.group(1)) if m else None


def _rel_tokens(node: LexborNode) -> set[str]:
    return set((_attr(node, "rel") or "").lower().split())


def _in_svg(node: LexborNode) -> bool:
    p = node.parent
    while p is not None:
        if p.tag == "svg":
            return True
        p = p.parent
    return False


def _jsonld_walk(obj, out: list[dict]) -> None:
    if isinstance(obj, list):
        for x in obj:
            _jsonld_walk(x, out)
    elif isinstance(obj, dict):
        if "@graph" in obj and isinstance(obj["@graph"], list):
            for x in obj["@graph"]:
                _jsonld_walk(x, out)
        else:
            out.append(obj)


def _types(item: dict) -> list[str]:
    t = item.get("@type") or item.get("type")
    if isinstance(t, list):
        return [str(x) for x in t]
    return [str(t)] if t else []


def parse_page(page: PageData, html: str, scope: SiteScope) -> ShellSignals:
    """Fill ``page`` from HTML. Returns signals used to decide whether to JS-render."""
    tree = LexborHTMLParser(html)
    url = page.final_url or page.url
    base = url
    for b in tree.css("base[href]"):
        nb = normalize_url(_attr(b, "href") or "", url)
        if nb:
            base = nb
        break

    # ------------------------------------------------------------- <head> signals
    titles = [t for t in tree.css("title") if not _in_svg(t)]
    page.title_count = len(titles)
    page.title = _clean(titles[0].text()) if titles else None

    descs: list[str] = []
    robots: list[str] = []
    for m in tree.css("meta"):
        name = (_attr(m, "name") or "").lower().strip()
        prop = (_attr(m, "property") or "").lower().strip()
        content = _attr(m, "content")
        if name == "description":
            descs.append(_clean(content))
        elif name in ("robots", "googlebot") and content:
            robots.append(content.strip() if name == "robots" else f"googlebot: {content.strip()}")
        elif name == "viewport":
            page.viewport = (content or "").strip()
        elif name == "author" and content:
            page.authors.append(_clean(content))
        elif (_attr(m, "http-equiv") or "").lower() == "refresh":
            page.meta_refresh = content
        if prop.startswith("og:") and content is not None:
            page.og.setdefault(prop[3:], _clean(content))
        if name.startswith("twitter:") and content is not None:
            page.twitter.setdefault(name[8:], _clean(content))
        if prop in ("article:published_time", "article:modified_time") and content:
            page.dates.setdefault(prop.split(":")[1], content.strip())
    page.description_count = len(descs)
    page.meta_description = descs[0] if descs else None
    page.meta_robots = ", ".join(robots) if robots else None

    page.canonicals = []
    page.hreflang = []
    for link in tree.css("link[rel]"):
        rels = _rel_tokens(link)
        href = _attr(link, "href")
        if "canonical" in rels:
            n = normalize_url(href or "", base)
            if n:
                page.canonicals.append(n)
            parent = link.parent
            while parent is not None and parent.tag not in ("head", "body"):
                parent = parent.parent
            if parent is not None and parent.tag == "body":
                page.canonical_in_body = True
        if "alternate" in rels and _attr(link, "hreflang"):
            n = normalize_url(href or "", base)
            page.hreflang.append({"lang": (_attr(link, "hreflang") or "").strip(), "href": n,
                                  "valid_code": bool(_HREFLANG.match((_attr(link, "hreflang") or "").strip()))})
        if "author" in rels and href:
            page.authors.append(href)
    page.canonical = page.canonicals[0] if page.canonicals else None

    html_node = tree.css_first("html")
    page.lang = (_attr(html_node, "lang") or "").strip() or None if html_node else None

    # ------------------------------------------------------------- structured data
    jsonld: list[dict] = []
    sd_errors: list[str] = []
    for s in tree.css("script"):
        stype = (_attr(s, "type") or "").strip().lower()
        if stype != "application/ld+json":
            continue
        raw = (s.text(deep=True) or "").strip()
        if not raw:
            sd_errors.append("Empty JSON-LD block")
            continue
        try:
            _jsonld_walk(json.loads(raw), jsonld)
        except json.JSONDecodeError as e:
            sd_errors.append(f"JSON-LD syntax error: {e.msg} (line {e.lineno}, col {e.colno})")
    microdata: list[dict] = []
    rdfa: list[dict] = []
    if "itemscope" in html or "typeof=" in html or "vocab=" in html:
        try:
            import extruct
            data = extruct.extract(html, base_url=url, syntaxes=["microdata", "rdfa"], uniform=True,
                                   errors="ignore")
            microdata = data.get("microdata", [])[:50]
            rdfa = [x for x in data.get("rdfa", []) if isinstance(x, dict) and x.get("@type")][:50]
        except Exception as e:  # extruct can choke on odd markup
            sd_errors.append(f"Microdata/RDFa parse error: {type(e).__name__}")
    page.structured_data = {
        "jsonld": jsonld[:100], "microdata": microdata, "rdfa": rdfa, "errors": sd_errors,
        "types": sorted({t for item in jsonld + microdata + rdfa for t in _types(item)}),
    }
    for item in jsonld:
        for key in ("datePublished", "dateModified"):
            if isinstance(item.get(key), str):
                page.dates.setdefault(key, item[key])
        a = item.get("author")
        for x in (a if isinstance(a, list) else [a]):
            if isinstance(x, dict) and x.get("name"):
                page.authors.append(_clean(str(x["name"])))
            elif isinstance(x, str):
                page.authors.append(_clean(x))
        if any(t in ("LocalBusiness", "Organization") or t.endswith("Business") for t in _types(item)):
            nap = page.nap.setdefault("schema", {})
            for k in ("name", "telephone", "address"):
                if k in item and k not in nap:
                    nap[k] = item[k]

    # ------------------------------------------------------------- links
    page.links = []
    https_page = url.startswith("https://")
    for a in tree.css("a[href]"):
        href = (_attr(a, "href") or "").strip()
        if href.lower().startswith("tel:"):
            page.nap.setdefault("tel_links", [])
            num = href[4:].strip()
            if num not in page.nap["tel_links"]:
                page.nap["tel_links"].append(num)
            continue
        target = normalize_url(href, base)
        if not target:
            continue
        anchor = _clean(a.text(deep=True))
        img_link = False
        if not anchor:
            img = a.css_first("img[alt]")
            if img is not None:
                anchor = _clean(_attr(img, "alt"))
                img_link = True
        if not anchor:
            anchor = _clean(_attr(a, "aria-label") or _attr(a, "title"))
        rels = _rel_tokens(a)
        page.links.append(LinkRec(target=target, anchor=anchor[:300], internal=scope.is_internal(target),
                                  nofollow="nofollow" in rels, sponsored="sponsored" in rels,
                                  ugc="ugc" in rels, image_link=img_link))

    # ------------------------------------------------------------- images
    page.images = []
    for i, img in enumerate(tree.css("img")):
        src = _attr(img, "src") or ""
        lazy_src = _attr(img, "data-src") or _attr(img, "data-lazy-src") or _attr(img, "data-original")
        if lazy_src and (not src or src.startswith("data:")):
            src = lazy_src
        if not src or src.startswith("data:"):
            continue
        absrc = normalize_url(src, base)
        if not absrc:
            continue
        parent = img.parent
        modern = False
        if parent is not None and parent.tag == "picture":
            for s in parent.css("source[type]"):
                if (_attr(s, "type") or "").lower() in ("image/webp", "image/avif"):
                    modern = True
        in_link = False
        p = img.parent
        while p is not None and p.tag not in ("body", "html"):
            if p.tag == "a":
                in_link = True
                break
            p = p.parent
        page.images.append(ImageRec(
            src=absrc, alt=_attr(img, "alt") if "alt" in img.attributes else None,
            filename=filename_of(absrc), width_attr=_int(_attr(img, "width")),
            height_attr=_int(_attr(img, "height")), loading=(_attr(img, "loading") or None),
            position=i, has_srcset=bool(_attr(img, "srcset") or _attr(img, "data-srcset")),
            modern_source=modern, in_link=in_link))

    # ------------------------------------------------------------- mixed content & resources
    page.mixed_content = []
    page.resources = []
    resource_nodes = [("img", "src"), ("script", "src"), ("iframe", "src"), ("video", "src"),
                      ("audio", "src"), ("source", "src"), ("embed", "src"), ("object", "data")]
    for tag, attr in resource_nodes:
        for n in tree.css(f"{tag}[{attr}]"):
            r = normalize_url(_attr(n, attr) or "", base)
            if not r:
                continue
            if https_page and r.startswith("http://"):
                page.mixed_content.append(r)
            if tag == "script" and scope.is_internal(r):
                page.resources.append(r)
    for link in tree.css("link[href]"):
        if "stylesheet" in _rel_tokens(link):
            r = normalize_url(_attr(link, "href") or "", base)
            if r:
                if https_page and r.startswith("http://"):
                    page.mixed_content.append(r)
                if scope.is_internal(r):
                    page.resources.append(r)
    page.mixed_content = sorted(set(page.mixed_content))[:100]
    page.resources = sorted(set(page.resources))[:100]

    # ------------------------------------------------------------- headings
    page.headings = []
    for h in tree.css("h1, h2, h3, h4, h5, h6"):
        page.headings.append((int(h.tag[1]), _clean(h.text(deep=True))[:300]))

    # ------------------------------------------------------------- byline / address heuristics
    for sel in ('[rel="author"]', '[itemprop="author"]', ".author", ".byline", '[class*="byline"]'):
        for n in tree.css(sel)[:3]:
            t = _clean(n.text(deep=True))
            if t and len(t) < 120:
                page.authors.append(t)
    for t in tree.css("time[datetime]")[:3]:
        page.dates.setdefault("time_element", (_attr(t, "datetime") or "").strip())
    addr = tree.css_first("address")
    if addr is not None:
        page.nap["address_element"] = _clean(addr.text(deep=True))[:300]
    page.authors = list(dict.fromkeys(a for a in page.authors if a))[:10]

    # ------------------------------------------------------------- shell signals, visible text
    script_count = len(tree.css("script[src]"))
    mount_empty = False
    for mid in MOUNT_IDS:
        n = tree.css_first(f"#{mid}")
        if n is not None and not _clean(n.text(deep=True)):
            mount_empty = True
    noscript_js = any("javascript" in (n.text(deep=True) or "").lower() for n in tree.css("noscript"))

    tree.strip_tags(STRIP_FOR_TEXT)
    body = tree.body
    text = _clean(body.text(deep=True, separator=" ")) if body is not None else ""
    page.text = text
    words = tokens(text)
    page.word_count = len(words)
    page.content_hash = content_hash(text) if text else None
    page.simhash = simhash(text) if text else None
    phones = {m.group(1).strip() for m in _PHONE.finditer(text)
              if 9 <= sum(c.isdigit() for c in m.group(1)) <= 15}
    if phones:
        page.nap["text_phones"] = sorted(phones)[:5]

    return ShellSignals(word_count=page.word_count, script_count=script_count,
                        mount_empty=mount_empty, noscript_js=noscript_js)
