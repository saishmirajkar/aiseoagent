"""Turn an audit's findings into concrete, selectable fixes.

kind:
  auto    – mechanical change, checked in code (titles, descriptions, alt text, sizes, canonical, labels…)
  review  – the AI writes text; the pull request asks a person to read and edit it
  input   – needs facts only the owner knows; skipped until answered
  manual  – can't be done safely in code (redirects, server settings, image compression…): listed as
            instructions in the pull request
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any

from app.reports.plain import P, agent_of

TITLE_CHECKS = {"meta.title_missing", "meta.title_duplicate", "meta.title_too_long", "meta.title_too_short",
                "meta.keyword_in_title", "meta.intent_alignment", "meta.title_multiple"}
DESC_CHECKS = {"meta.description_missing", "meta.description_duplicate", "meta.description_length",
               "meta.intent_alignment", "meta.description_multiple"}
HANDLED = TITLE_CHECKS | DESC_CHECKS | {
    "img.alt_missing", "img.alt_generation", "img.alt_empty", "img.missing_dimensions", "tech.canonical_missing",
    "meta.social_tags", "tech.lang", "tech.viewport", "schema.missing_types", "crawl.sitemap_present",
    "crawl.robots_sitemap_directive", "ai.llms_txt", "geo.answer_first", "geo.faq", "content.gaps",
    "content.h1_missing", "geo.fact_consistency", "content.placeholder", "links.suggestions"}
MAX_LINKS = 10


@dataclass
class FixItem:
    id: str
    check_id: str
    agent: str
    agent_icon: str
    kind: str
    action: str
    label: str
    page_url: str | None = None
    before: str | None = None
    after: str | None = None
    data: dict[str, Any] = field(default_factory=dict)
    questions: list[dict[str, Any]] = field(default_factory=list)

    def to_json(self) -> dict:
        return asdict(self)


def _id(action: str, *parts: Any) -> str:
    return f"{action}:{hashlib.sha1(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:10]}"


def _item(f: dict, kind: str, action: str, label: str, **kw) -> FixItem:
    a = agent_of(f["module"])
    return FixItem(id=_id(action, f["check_id"], kw.get("page_url"), kw.get("data", {}).get("key")),
                   check_id=f["check_id"], agent=a["name"], agent_icon=a["icon"], kind=kind, action=action,
                   label=label, **kw)


def _pick(options: list[dict] | None) -> str | None:
    if not options:
        return None
    fits = [o for o in options if o.get("fits", True) and o.get("text")]
    return (fits or options)[0].get("text")


def _h1(page: dict) -> str | None:
    return next((t for lvl, t in (page.get("data") or {}).get("headings", []) if lvl == 1 and t), None)


def build_plan(findings: list[dict], pages: list[dict], audit_input: dict) -> list[FixItem]:
    by_check: dict[str, list[dict]] = {}
    for f in findings:
        by_check.setdefault(f["check_id"], []).append(f)
    page_by_url = {p["url"]: p for p in pages}
    items: list[FixItem] = []
    brand = audit_input.get("brand") or ""

    def problems(checks: set[str]) -> dict[str, tuple[dict, dict]]:
        """url -> (finding, item) for failing/warning items of these checks."""
        out = {}
        for cid in sorted(checks):
            for f in by_check.get(cid, []):
                if f["status"] not in ("FAIL", "WARNING"):
                    continue
                for it in f["items"]:
                    if it.get("url"):
                        out.setdefault(it["url"], (f, it))
        return out

    # ---- Google headline and summary (rewrites written during the audit)
    rewrites: dict[str, dict] = {}
    for cid in sorted(TITLE_CHECKS | DESC_CHECKS):
        for f in by_check.get(cid, []):
            for it in f["items"]:
                gf = it.get("generated_fix") or {}
                if gf.get("type") == "rewrite" and it.get("url"):
                    rewrites.setdefault(it["url"], gf)
    title_bad, desc_bad = problems(TITLE_CHECKS), problems(DESC_CHECKS)
    new_titles: dict[str, str] = {}
    new_descs: dict[str, str] = {}
    for url, gf in sorted(rewrites.items()):
        page = page_by_url.get(url, {})
        t, d = _pick(gf.get("titles")), _pick(gf.get("descriptions"))
        if url in title_bad and t and t != page.get("title"):
            new_titles[url] = t
            items.append(_item(title_bad[url][0], "auto", "set_title", f"New Google headline for {_path(url)}",
                               page_url=url, before=page.get("title"), after=t, data={"key": url, "value": t}))
        if url in desc_bad and d and d != page.get("meta_description"):
            new_descs[url] = d
            items.append(_item(desc_bad[url][0], "auto", "set_description", f"New Google summary for {_path(url)}",
                               page_url=url, before=page.get("meta_description"), after=d,
                               data={"key": url, "value": d}))

    # ---- image descriptions and sizes (one change per image file, applied wherever the file is used)
    alts: dict[str, dict] = {}
    for cid in ("img.alt_generation", "img.alt_missing", "img.alt_empty"):
        for f in by_check.get(cid, []):
            for it in f["items"]:
                gf, ev = it.get("generated_fix") or {}, it.get("evidence") or {}
                if gf.get("type") == "alt_text" and ev.get("src") and ev["src"] not in alts:
                    alt = "" if gf.get("decorative") else ev.get("suggested_alt") or _alt_from_code(gf.get("code"))
                    if alt is not None:
                        alts[ev["src"]] = {"f": f, "alt": alt, "pages": [it["url"], *it.get("also_on", [])]}
    for src, a in sorted(alts.items()):
        label = (f"Mark decorative image {_file(src)} (empty alt)" if a["alt"] == ""
                 else f"Image description for {_file(src)}")
        items.append(_item(a["f"], "auto", "img_attrs", label, after=a["alt"] or '(alt="")',
                           data={"key": src, "src": src, "alt": a["alt"], "pages": a["pages"][:50]}))
    for f in by_check.get("img.missing_dimensions", []):
        if f["status"] not in ("FAIL", "WARNING"):
            continue
        for it in f["items"]:
            gf, ev = it.get("generated_fix") or {}, it.get("evidence") or {}
            m = re.search(r'width="(\d+)"\s+height="(\d+)"', gf.get("code") or "")
            if m and ev.get("src"):
                items.append(_item(f, "auto", "img_attrs", f"Set width and height on {_file(ev['src'])}",
                                   after=f"{m.group(1)}×{m.group(2)}",
                                   data={"key": "dims:" + ev["src"], "src": ev["src"], "width": int(m.group(1)),
                                         "height": int(m.group(2)), "pages": [it["url"], *it.get("also_on", [])][:50]}))

    # ---- head tags
    for url, (f, _) in sorted(problems({"tech.canonical_missing"}).items()):
        items.append(_item(f, "auto", "canonical", f"Add canonical tag to {_path(url)}", page_url=url, after=url,
                           data={"key": url, "href": url}))
    lang = audit_input.get("language") or _common_lang(pages)
    for url, (f, _) in sorted(problems({"tech.lang"}).items()):
        q = [] if lang else [{"id": "site_language", "question": "Which language is the website written in? "
                                                                 "(a code like en, en-IN, hi)", "required": True}]
        items.append(_item(f, "auto" if lang else "input", "lang", f"Set page language on {_path(url)}", page_url=url,
                           after=lang, data={"key": url, "lang": lang}, questions=q))
    for url, (f, _) in sorted(problems({"tech.viewport"}).items()):
        items.append(_item(f, "auto", "viewport", f"Make {_path(url)} fit mobile screens", page_url=url,
                           data={"key": url}))
    for url, (f, it) in sorted(problems({"meta.social_tags"}).items()):
        page = page_by_url.get(url, {})
        og = (page.get("data") or {}).get("og") or {}
        tags = {"og:title": new_titles.get(url) or page.get("title"),
                "og:description": new_descs.get(url) or page.get("meta_description"), "og:url": url}
        tags = {k: v for k, v in tags.items() if v and k.split(":")[1] in (it["evidence"].get("missing_og") or [])
                or k == "og:url"}
        q = []
        if "image" in (it["evidence"].get("missing_og") or []) and not og.get("image"):
            q.append({"id": "og_image", "question": "Image to show when pages are shared on WhatsApp/social media: "
                      "full link to a 1200×630 picture (optional)", "required": False})
        items.append(_item(f, "auto", "social_tags", f"Sharing preview tags for {_path(url)}", page_url=url,
                           data={"key": url, "tags": tags}, questions=q))

    # ---- structured data (generated during the audit; fields the page can't tell us are asked)
    for f in by_check.get("schema.missing_types", []):
        if f["status"] not in ("FAIL", "WARNING"):
            continue
        for it in f["items"]:
            gf = it.get("generated_fix") or {}
            if gf.get("type") != "jsonld" or not gf.get("code"):
                continue
            t = it["evidence"].get("missing_type")
            needs = [n for n in gf.get("needs_input") or [] if n not in ("geo",)]
            q = [{"id": f"schema:{t}:{n}", "question": f"{t} label: {n} (as shown to customers)",
                  "required": n in ("name", "address"), "field": n} for n in needs]
            items.append(_item(f, "input" if any(x["required"] for x in q) else "auto", "jsonld",
                               f"Add '{t}' label for Google to {_path(it['url'])}", page_url=it["url"],
                               after=t, data={"key": f"{it['url']}:{t}", "code": gf["code"], "type": t},
                               questions=q))

    # ---- site files
    for f in by_check.get("crawl.sitemap_present", []):
        if f["status"] == "FAIL":
            urls = sorted(p["url"] for p in pages if p.get("indexable") and p.get("status_code") == 200)
            items.append(_item(f, "auto", "sitemap", "Create a page list for Google (sitemap.xml)",
                               after=f"{len(urls)} page{'' if len(urls) == 1 else 's'}",
                               data={"key": "sitemap", "urls": urls}))
    for f in by_check.get("crawl.robots_sitemap_directive", []):
        if f["status"] in ("FAIL", "WARNING"):
            items.append(_item(f, "auto", "robots_sitemap", "Point robots.txt to the sitemap",
                               data={"key": "robots"}))
    for f in by_check.get("ai.llms_txt", []):
        if f["status"] in ("FAIL", "WARNING"):
            items.append(_item(f, "review", "llms_txt", "Create an llms.txt guide for AI assistants",
                               data={"key": "llms"}))

    # ---- writing (AI-written, reviewed by a person)
    for f in by_check.get("geo.answer_first", []):
        for it in f["items"] if f["status"] in ("FAIL", "WARNING") else []:
            text = (it.get("generated_fix") or {}).get("code")
            if text and it.get("url"):
                items.append(_item(f, "review", "write_opening", f"Answer-first opening for {_path(it['url'])}",
                                   page_url=it["url"], after=text, data={"key": it["url"], "draft": text}))
    faqs: dict[str, dict] = {}
    for f in by_check.get("content.gaps", []) + by_check.get("geo.faq", []):
        for it in f["items"] if f["status"] in ("FAIL", "WARNING") else []:
            gf = it.get("generated_fix") or {}
            if f["check_id"] == "geo.faq":
                pairs = _faq_pairs(gf.get("code") or "")
            else:  # content brief: unanswered ones are marked "(to be written by the site owner)"
                pairs = [(q.get("q") or q.get("question"),
                          "" if str(q.get("a") or q.get("answer") or "").startswith("(") else (q.get("a") or q.get("answer")))
                         for q in (gf.get("faqs") or it["evidence"].get("faqs") or [])]
            if it.get("url") and pairs:
                slot = faqs.setdefault(it["url"], {"f": f, "pairs": {}})
                for q, a in pairs:
                    if q:
                        slot["pairs"].setdefault(q, a or "")
            if f["check_id"] == "content.gaps" and it.get("url"):
                for add in [a for a in _additions(gf.get("code") or "")
                            if not re.search(r"\b(faq|questions)\b", a["heading"], re.I)]:  # the FAQ item covers it
                    items.append(_item(f, "input", "write_section", f"New section '{add['heading']}' on "
                                       f"{_path(it['url'])}", page_url=it["url"], after=add["heading"],
                                       data={"key": f"{it['url']}:{add['heading']}", **add},
                                       questions=[{"id": _id("q", it["url"], add["heading"]),
                                                   "question": f"For the new section \"{add['heading']}\" on "
                                                   f"{_path(it['url'])}: tell us the facts in your own words "
                                                   f"({add['what']})", "required": True}]))
    for url, slot in sorted(faqs.items()):
        answered = {q: a for q, a in slot["pairs"].items() if a}
        unanswered = [q for q, a in slot["pairs"].items() if not a]
        qs = [{"id": _id("faq", url, q), "question": f"FAQ on {_path(url)}: {q}", "required": False, "faq": q}
              for q in unanswered[:5]]
        if answered or qs:
            items.append(_item(slot["f"], "review", "write_faq", f"Questions & answers section on {_path(url)}",
                               page_url=url, after=f"{len(answered)} answered, {len(unanswered)} for you to answer",
                               data={"key": url, "pairs": answered}, questions=qs))
    for url, (f, _) in sorted(problems({"content.h1_missing"}).items()):
        page = page_by_url.get(url, {})
        proposal = _strip_brand(new_titles.get(url) or page.get("title") or "", brand)
        items.append(_item(f, "review", "set_h1", f"Main heading for {_path(url)}", page_url=url,
                           after=proposal, data={"key": url, "h1": proposal}))
    for f in by_check.get("geo.fact_consistency", []):
        for it in f["items"] if f["status"] in ("FAIL", "WARNING") else []:
            ev = it["evidence"]
            qid = _id("fact", it["url"], ev.get("problem"))
            items.append(_item(f, "input", "fact_fix", f"Fix contradictory fact: {ev.get('problem')}",
                               page_url=it["url"], before=ev.get("found_text") or ", ".join(ev.get("mentions", [])),
                               data={"key": qid, "problem": ev.get("problem"), "evidence": ev},
                               questions=[{"id": qid, "question": f"{ev.get('problem')}. What is correct?",
                                           "required": True}]))
    for f in by_check.get("content.placeholder", []):
        for it in f["items"] if f["status"] in ("FAIL", "WARNING") else []:
            qid = _id("ph", it["url"])
            items.append(_item(f, "input", "replace_placeholder", f"Replace placeholder text on {_path(it['url'])}",
                               page_url=it["url"], before=it["evidence"].get("found_text"),
                               data={"key": qid, "found": it["evidence"].get("found_text")},
                               questions=[{"id": qid, "question": f"What should replace the placeholder text on "
                                           f"{_path(it['url'])}? Write 'remove' to delete it.", "required": True}]))
    for f in by_check.get("links.suggestions", []):
        for it in (f["items"] if f["status"] in ("FAIL", "WARNING") else [])[:MAX_LINKS]:
            ev = it.get("evidence") or {}
            if ev.get("add_link_to") and ev.get("anchor") and ev.get("ai_keep", True) is not False:
                items.append(_item(f, "review", "add_link", f"Link {_path(it['url'])} → {_path(ev['add_link_to'])}",
                                   page_url=it["url"], after=ev["anchor"],
                                   data={"key": f"{it['url']}>{ev['add_link_to']}", "target": ev["add_link_to"],
                                         "anchor": ev["anchor"]}))

    # ---- everything else: instructions for a developer, listed in the pull request
    for f in findings:
        if f["status"] in ("FAIL", "WARNING") and f["check_id"] not in HANDLED:
            items.append(_item(f, "manual", "manual", f.get("plain_title") or f["title"],
                               data={"key": f["check_id"], "found": f.get("plain_found"), "do": f.get("plain_do"),
                                     "fix": f.get("fix"), "urls": [i.get("url") for i in f["items"][:10] if i.get("url")]}))
    return _dedupe(items)


def _dedupe(items: list[FixItem]) -> list[FixItem]:
    """One change per page: /x and /x/ are the same page, and page 2+ of a listing gets no page-level edits."""
    from app.crawler.urls import is_pagination
    seen: set[tuple[str, str]] = set()
    out = []
    for it in items:
        if it.page_url and it.kind != "manual":
            if is_pagination(it.page_url) or re.search(r"[?&]page=1$", it.page_url):
                continue
            key = (it.action, str(it.data.get("type", "")) + str(it.data.get("heading", "")) + str(it.data.get("target", "")),
                   it.page_url.rstrip("/"))
            if key in seen:
                continue
            seen.add(key)
        out.append(it)
    return out


# ------------------------------------------------------------------ helpers
def _path(url: str | None) -> str:
    if not url:
        return ""
    m = re.match(r"https?://[^/]+(/.*)?$", url)
    return (m.group(1) or "/") if m else url


def _file(src: str) -> str:
    return src.split("?")[0].rsplit("/", 1)[-1][:60]


def _alt_from_code(code: str | None) -> str | None:
    m = re.search(r'alt="([^"]*)"', code or "")
    return m.group(1) if m else None


def _common_lang(pages: list[dict]) -> str | None:
    langs = [(p.get("data") or {}).get("lang") for p in pages if (p.get("data") or {}).get("lang")]
    return max(set(langs), key=langs.count).split("-")[0] if langs else None


def _faq_pairs(code: str) -> list[tuple[str, str]]:
    return [(q.strip(), a.strip()) for q, a in re.findall(r"Q:\s*(.+?)\nA:\s*(.*?)(?:\n\n|\Z)", code, re.S)]


def _additions(code: str) -> list[dict]:
    out = []
    for block in re.split(r"(?m)^## ", code):
        if not block.strip():
            continue
        head, _, body = block.partition("\n")
        out.append({"heading": head.strip(), "what": body.strip()[:400]})
    return out[:4]


def _strip_brand(title: str, brand: str) -> str:
    t = re.split(r"\s+[|–—-]\s+", title)[0].strip() if title else ""
    return t if t and t.lower() != brand.lower() else title
