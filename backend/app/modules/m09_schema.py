"""Module 9: Schema Markup — detect, validate, and generate JSON-LD.

Page types are detected with deterministic signals (URL patterns, og:type, dates, prices,
video embeds). The LLM layer (Phase 3) refines detection and drafts FAQ content.
Every generated JSON-LD block is validated in code; fields we cannot know from the page
(e.g. a price that is not published) are listed under ``needs_input`` instead of being invented.
"""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from urllib.parse import parse_qs, urlsplit

from app.modules.base import (
    AuditContext, Category, CheckModule, CheckResult, CheckSpec, FindingItem, Level, Need, Status,
)
from app.modules.schema_rules import canonical_type, is_valid_jsonld, item_name, types_of, validate, SINGLETONS
from app.sitemodel.types import PageData, SiteModel

H, M, L = Level.HIGH, Level.MED, Level.LOW
SOCIAL = ("facebook.com", "instagram.com", "linkedin.com", "youtube.com", "x.com", "twitter.com",
          "tiktok.com", "pinterest.com", "threads.net", "github.com", "wikipedia.org", "crunchbase.com")
ARTICLE_PATH = re.compile(r"/(blog|blogs|news|article|articles|post|posts|insights|stories|journal|"
                          r"resources|guides?|learn)/[^/]+", re.I)
PRODUCT_PATH = re.compile(r"/(product|products|shop|store|item|items|p)/[^/]+", re.I)
PRICE = re.compile(r"(₹|Rs\.?\s?|INR\s?|\$|USD\s?|€|EUR\s?|£|GBP\s?)\s?(\d{1,3}(?:[,\s]\d{2,3})*(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)")
CURRENCY = {"₹": "INR", "rs": "INR", "inr": "INR", "$": "USD", "usd": "USD", "€": "EUR", "eur": "EUR",
            "£": "GBP", "gbp": "GBP"}
CART = re.compile(r"\b(add to cart|add to bag|buy now|add to basket|in stock|out of stock)\b", re.I)
TITLE_SPLIT = re.compile(r"\s+[|–—\-:·•]\s+")
YT_ID = re.compile(r"(?:youtube(?:-nocookie)?\.com/(?:embed|shorts|v)/|youtu\.be/)([\w-]{11})")


# ---------------------------------------------------------------------------- site facts
def site_name(site: SiteModel, ctx: AuditContext) -> str | None:
    home = site.homepage
    if ctx.inputs.brand:
        return ctx.inputs.brand
    if ctx.inputs.business_name:
        return ctx.inputs.business_name
    if home and home.og.get("site_name"):
        return home.og["site_name"]
    if home and home.title:
        return TITLE_SPLIT.split(home.title)[0].strip() or None
    return None


def social_profiles(site: SiteModel, brand: str | None) -> tuple[list[str], list[str]]:
    """(confident, unconfirmed) social profile URLs for sameAs.

    Confident = linked from most pages (site-wide header/footer) or the handle matches the brand/domain.
    A social link that appears once in body content may belong to a client or partner.
    """
    home = site.homepage
    pages = site.html_pages()
    counts: Counter[str] = Counter()
    for p in pages:
        counts.update({l.target for l in p.links if not l.internal})
    tokens = {re.sub(r"[^a-z0-9]", "", x.lower()) for x in (brand or "", site.facts.host.split(".")[-2] if "." in site.facts.host else "")}
    tokens = {t for t in tokens if len(t) >= 3}
    sure, unsure = [], []
    for l in (home.links if home else []):
        host = urlsplit(l.target).hostname or ""
        if l.internal or not any(host == d or host.endswith("." + d) for d in SOCIAL):
            continue
        path = urlsplit(l.target).path.strip("/")
        if not path or path.startswith(("sharer", "share", "intent", "embed", "watch")):
            continue
        if l.target in sure or l.target in unsure:
            continue
        handle = re.sub(r"[^a-z0-9]", "", path.lower())
        sitewide = len(pages) >= 3 and counts[l.target] >= 0.8 * len(pages)
        if sitewide or any(t in handle or handle in t for t in tokens if handle):
            sure.append(l.target)
        else:
            unsure.append(l.target)
    return sure[:10], unsure[:10]


def unique_description(page: PageData, site: SiteModel) -> str | None:
    """The page's meta description, unless it is shared with other pages (then it describes nothing)."""
    d = page.meta_description
    if not d:
        return None
    same = sum(1 for p in site.html_pages() if p.meta_description == d)
    return d if same <= 1 else None


def logo_url(site: SiteModel) -> str | None:
    home = site.homepage
    for img in (home.images if home else []):
        if "logo" in (img.src + (img.alt or "") + img.filename).lower():
            return img.src
    return None


def looks_local(site: SiteModel, ctx: AuditContext) -> bool:
    if ctx.inputs.address or ctx.inputs.phone:
        return True
    for p in site.html_pages():
        if any(canonical_type(t) == "LocalBusiness" for t in p.structured_data.get("types", [])):
            return True
    home = site.homepage
    return bool(home and home.nap.get("address_element") and (home.nap.get("tel_links") or home.nap.get("text_phones")))


# ---------------------------------------------------------------------------- page types
def detect_types(page: PageData, site: SiteModel, local: bool) -> list[tuple[str, str]]:
    """Expected schema types for a page, with the reason each was inferred."""
    out: list[tuple[str, str]] = []
    path = urlsplit(page.url).path
    if page.url == site.facts.start_url or path in ("", "/"):
        out.append(("LocalBusiness" if local else "Organization", "homepage"))
        out.append(("WebSite", "homepage"))
        return out
    og_type = (page.og.get("type") or "").lower()
    has_price = bool(PRICE.search(page.text[:20000]))  # with an add-to-cart signal = product page
    if og_type == "product" or PRODUCT_PATH.search(path) or (has_price and CART.search(page.text[:20000])):
        out.append(("Product", "og:type product" if og_type == "product" else "product URL"))
    elif og_type == "article" or "datePublished" in page.dates or "published_time" in page.dates or (
            ARTICLE_PATH.search(path) and page.word_count >= 300):
        out.append(("Article", "article signals (og:type/dates/URL)"))
    if page.embeds:
        out.append(("VideoObject", f"{len(page.embeds)} video embed(s)"))
    if len([s for s in path.split("/") if s]) >= 2:
        out.append(("BreadcrumbList", "nested URL path"))
    return out


def _satisfied(expected: str, present: set[str]) -> bool:
    if expected in present:
        return True
    if expected == "Organization" and "LocalBusiness" in present:
        return True
    return False


# ---------------------------------------------------------------------------- generation
def _headline(page: PageData) -> str | None:
    h1 = next((t for lvl, t in page.headings if lvl == 1 and t), None)
    return (h1 or (TITLE_SPLIT.split(page.title)[0] if page.title else None) or None)


def _image(page: PageData) -> str | None:
    if page.og.get("image"):
        return page.og["image"]
    big = [i for i in page.images if "logo" not in (i.src + (i.alt or "")).lower()]
    return big[0].src if big else None


def _slug_name(seg: str) -> str:
    return re.sub(r"[-_]+", " ", seg).strip().title()


def generate(t: str, page: PageData, site: SiteModel, ctx: AuditContext) -> dict:
    """Build JSON-LD for type ``t`` from page data. Returns {jsonld, needs_input, notes}."""
    name = site_name(site, ctx)
    origin = site.facts.origin
    needs: list[str] = []
    notes: list[str] = []
    obj: dict = {"@context": "https://schema.org", "@type": t}
    if t in ("Organization", "LocalBusiness"):
        if ctx.inputs.business_type and t == "LocalBusiness":
            notes.append(f"Consider a more specific LocalBusiness subtype for '{ctx.inputs.business_type}'.")
        obj["name"] = name or needs.append("name")
        obj["url"] = origin + "/"
        if logo_url(site):
            obj["logo"] = logo_url(site)
        if page.meta_description:
            obj["description"] = page.meta_description
        same, maybe = social_profiles(site, name)
        if same:
            obj["sameAs"] = same
        if maybe:
            notes.append("Add to sameAs only if these are your own profiles: " + ", ".join(maybe))
        phone = ctx.inputs.phone or next(iter(page.nap.get("tel_links", [])), None)
        if phone:
            obj["telephone"] = phone
        if t == "LocalBusiness":
            if ctx.inputs.address:
                obj["address"] = {"@type": "PostalAddress", "streetAddress": ctx.inputs.address}
                notes.append("Split the address into streetAddress, addressLocality, postalCode, addressCountry.")
            else:
                needs.append("address")
            if _image(page):
                obj["image"] = _image(page)
            needs += ["openingHoursSpecification", "geo"]
    elif t == "WebSite":
        obj["name"] = name or needs.append("name")
        obj["url"] = origin + "/"
    elif t == "Article":
        obj["@type"] = "BlogPosting" if re.search(r"/blogs?/", page.url) else "Article"
        obj["headline"] = (_headline(page) or "")[:110] or needs.append("headline")
        obj["mainEntityOfPage"] = page.url
        img = _image(page)
        obj["image"] = img if img else needs.append("image")
        dp = page.dates.get("datePublished") or page.dates.get("published_time") or page.dates.get("time_element")
        obj["datePublished"] = dp if dp else needs.append("datePublished")
        dm = page.dates.get("dateModified") or page.dates.get("modified_time")
        if dm:
            obj["dateModified"] = dm
        author = next((a for a in page.authors if "://" not in a and len(a.split()) <= 5), None)
        obj["author"] = {"@type": "Person", "name": author} if author else needs.append("author")
        if name:
            pub = {"@type": "Organization", "name": name}
            if logo_url(site):
                pub["logo"] = {"@type": "ImageObject", "url": logo_url(site)}
            obj["publisher"] = pub
    elif t == "Product":
        obj["name"] = _headline(page) or needs.append("name")
        img = _image(page)
        if img:
            obj["image"] = img
        desc = unique_description(page, site)
        if desc:
            obj["description"] = desc
        else:
            needs.append("description")
            if page.meta_description:
                notes.append("The page's meta description is shared with other pages; write a product-specific one.")
        if name:
            obj["brand"] = {"@type": "Brand", "name": name}
        m = PRICE.search(page.text[:20000])
        prices = {mm.group(0) for mm in PRICE.finditer(page.text[:20000])}
        if m and len(prices) == 1:
            cur = CURRENCY.get(m.group(1).strip().rstrip(".").lower(), None)
            amount = re.sub(r"[,\s]", "", m.group(2))
            offer = {"@type": "Offer", "price": amount, "url": page.url}
            if cur:
                offer["priceCurrency"] = cur
            else:
                needs.append("offers.priceCurrency")
            obj["offers"] = offer
            needs.append("offers.availability")
            notes.append(f"Price {m.group(0).strip()} detected on the page; confirm it is the product price.")
        else:
            needs += ["offers.price", "offers.priceCurrency"]
            if len(prices) > 1:
                notes.append("Several prices appear on the page; pick the product's own price.")
            else:
                notes.append("No price is shown on the page. Without offers/review/aggregateRating the "
                             "product cannot get price rich results; the rest of the markup still helps.")
    elif t == "BreadcrumbList":
        segs = [s for s in urlsplit(page.url).path.split("/") if s]
        crumbs = [("Home", origin + "/")]
        acc = ""
        for s in segs[:-1]:
            acc += "/" + s
            for cand in (origin + acc, origin + acc + "/"):
                p = site.pages.get(cand)
                if p is not None and p.is_ok:
                    crumbs.append((_headline(p) or _slug_name(s), cand))
                    break
        crumbs.append((_headline(page) or _slug_name(segs[-1]), page.url))
        obj["itemListElement"] = [{"@type": "ListItem", "position": i + 1, "name": n, "item": u}
                                  for i, (n, u) in enumerate(crumbs)]
        if len(crumbs) == 2 and len(segs) > 1:
            notes.append("Intermediate path levels are not live pages, so they are left out.")
    elif t == "VideoObject":
        src = page.embeds[0]
        obj["embedUrl"] = src
        m = YT_ID.search(src) or None
        vid = m.group(1) if m else (parse_qs(urlsplit(src).query).get("v") or [None])[0]
        if vid:
            obj["thumbnailUrl"] = f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"
            obj["contentUrl"] = f"https://www.youtube.com/watch?v={vid}"
        else:
            needs.append("thumbnailUrl")
        needs += ["name", "description", "uploadDate"]
        notes.append("Copy the video's title, description and upload date from the video platform.")
        if len(page.embeds) > 1:
            notes.append(f"The page has {len(page.embeds)} videos; add one VideoObject per video.")
    obj = {k: v for k, v in obj.items() if v is not None}
    valid, problems = is_valid_jsonld(obj)
    unresolved = [p for p in problems if not any(part in p for n in needs for part in n.split("."))]
    code = '<script type="application/ld+json">\n' + json.dumps(obj, indent=2, ensure_ascii=False) + "\n</script>"
    return {"type": "jsonld", "schema_type": obj["@type"], "code": code, "needs_input": needs,
            "notes": notes, "validated": not unresolved, "validation_problems": unresolved}


# ---------------------------------------------------------------------------- module
class SchemaMarkup(CheckModule):
    key = "schema"
    number = 9
    name = "Schema Markup"
    category = Category.SCHEMA
    needs = frozenset({Need.PAGES})
    checks = (
        CheckSpec("schema.syntax", "Structured data syntax and vocabulary errors", H, L,
                  "Invalid JSON-LD (or items without @type/@context) is ignored by search engines.",
                  "Fix the JSON syntax, use \"@context\": \"https://schema.org\" and give every item an @type.",
                  population="pages"),
        CheckSpec("schema.required", "Missing required schema properties", M, L,
                  "Items missing required properties are not eligible for rich results.",
                  "Add the listed required properties (see Google's structured data docs for each type).",
                  population="pages"),
        CheckSpec("schema.recommended", "Missing recommended schema properties", L, L,
                  "Recommended properties make rich results more complete and help AI systems understand the page.",
                  "Add the listed recommended properties where the information exists on the page.",
                  population="pages"),
        CheckSpec("schema.conflicts", "Duplicate or conflicting schema", M, L,
                  "Several conflicting items of the same type (e.g. two Organizations with different names) "
                  "make search engines ignore or misread them.",
                  "Keep one item per entity and make names/URLs consistent across the site.", population="pages"),
        CheckSpec("schema.missing_types", "Schema missing for the page type", M, L,
                  "Matching schema (Organization, Product, Article, Breadcrumb, Video…) enables rich results "
                  "and helps search engines and AI assistants understand each page.",
                  "Add the generated JSON-LD to the page (fill any fields listed under needs_input).",
                  population="pages", missing_label="Structured data missing on {affected} page(s)"),
    )

    async def run(self, ctx: AuditContext) -> list[CheckResult]:
        site = ctx.site
        pages = [p for p in site.html_pages() if not p.is_noindex]
        if not pages:
            return [self.not_applicable(c.id, "No indexable HTML pages") for c in self.checks]
        local = looks_local(site, ctx)
        syntax, required, recommended, conflicts, missing = [], [], [], [], []
        type_counts: Counter[str] = Counter()
        with_sd = 0
        for p in pages:
            sd = p.structured_data or {}
            items = [(i, True) for i in sd.get("jsonld", [])] + [(i, False) for i in sd.get("microdata", [])] \
                + [(i, False) for i in sd.get("rdfa", [])]
            if items or sd.get("errors"):
                with_sd += 1
            errs = list(sd.get("errors", []))
            req, rec = [], []
            present: set[str] = set()
            by_type: dict[str, list[dict]] = defaultdict(list)
            for item, is_jsonld in items:
                for t in types_of(item):
                    type_counts[t] += 1
                    ct = canonical_type(t)
                    if ct:
                        present.add(ct)
                        by_type[ct].append(item)
                for v in validate(item, jsonld=is_jsonld):
                    errs += [f"{v.type}: {e}" for e in v.errors]
                    if v.missing_required:
                        req.append({"type": v.type, "missing": v.missing_required, "name": item_name(item)})
                    if v.missing_recommended:
                        rec.append({"type": v.type, "missing": v.missing_recommended})
            if errs:
                syntax.append(FindingItem(p.url, {"errors": errs[:10]}))
            if req:
                required.append(FindingItem(p.url, {"items": req[:10]}))
            if rec:
                recommended.append(FindingItem(p.url, {"items": rec[:10]}))
            conf = []
            for ct, lst in by_type.items():
                if ct in SINGLETONS and len(lst) > 1:
                    names = {json.dumps(item_name(i), sort_keys=True, default=str) for i in lst}
                    serial = {json.dumps(i, sort_keys=True, default=str) for i in lst}
                    if len(serial) == 1:
                        conf.append({"type": ct, "problem": f"{len(lst)} identical copies"})
                    elif len(names) > 1:
                        conf.append({"type": ct, "problem": "different names", "names": sorted(names)[:5]})
            if conf:
                conflicts.append(FindingItem(p.url, {"conflicts": conf}))
            for t, why in detect_types(p, site, local):
                if not _satisfied(t, present):
                    fix = generate(t, p, site, ctx)
                    missing.append(FindingItem(p.url, {"missing_type": t, "detected_because": why,
                                                       "present_types": sorted(present) or None},
                                               generated_fix=fix))
        n = len(pages)
        ctx.shared.setdefault("metrics", []).append(
            ("schema", "structured_data_coverage",
             {"pages": n, "pages_with_structured_data": with_sd, "types": dict(type_counts.most_common(30)),
              "local_business_detected": local}, "Crawl"))
        out = [
            self.by_population("schema.syntax", syntax, n, summary="Pages with invalid structured data"),
            self.by_population("schema.required", required, n, summary="Items missing required properties"),
            self.by_population("schema.recommended", recommended, n, fail=False,
                               summary="Items missing recommended properties"),
            self.by_population("schema.conflicts", conflicts, n, fail=False,
                               summary="Pages with duplicate or conflicting items"),
        ]
        r = self.by_population("schema.missing_types", missing, n, fail=False,
                               summary=f"{len(missing)} missing schema item(s) on {len({i.url for i in missing})} page(s); "
                                       f"{with_sd} of {n} pages have any structured data")
        if r.status == Status.PASS:
            r.summary = f"Every detected page type has matching schema ({with_sd} of {n} pages have structured data)"
        out.append(r)
        return out
