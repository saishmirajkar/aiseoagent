"""Compact page summaries for the LLM (never raw HTML) and boilerplate-aware main text.

Main text = visible text minus sentences that repeat across many pages (navigation, footers,
sidebars, template placeholder text). The same analysis feeds the deterministic
"repeated text blocks" content check.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from app.modules.common import is_utility
from app.sitemodel.types import PageData, SiteModel

_SENT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9“\"'(])|\s{2,}|\s[•|·]\s")
_NORM = re.compile(r"[^\w]+", re.UNICODE)
MIN_SENT_WORDS = 6


def sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENT.split(text) if s.strip()]


def _key(sent: str) -> str:
    return _NORM.sub(" ", sent.lower()).strip()


@dataclass
class BoilerplateAnalysis:
    main_text: dict[str, str] = field(default_factory=dict)
    repeated_share: dict[str, float] = field(default_factory=dict)  # share of a page's words that repeat
    blocks: list[dict] = field(default_factory=list)  # repeated long passages: {text, pages, words}


def analyse(site: SiteModel, *, sitewide_share: float = 0.3, section_share: float = 0.5,
            min_pages: int = 3) -> BoilerplateAnalysis:
    pages = [p for p in site.html_pages() if p.text]
    out = BoilerplateAnalysis()
    if not pages:
        return out
    sents = {p.url: sentences(p.text) for p in pages}
    df: Counter[str] = Counter()
    section_df: dict[str, Counter[str]] = defaultdict(Counter)
    section_n: Counter[str] = Counter()
    for p in pages:
        keys = {_key(s) for s in sents[p.url] if len(s.split()) >= 3}
        df.update(keys)
        sec = p.template_key or "/"
        section_df[sec].update(keys)
        section_n[sec] += 1
    n = len(pages)
    pages_by_key: dict[str, list[str]] = defaultdict(list)
    for p in pages:
        sec = p.template_key or "/"
        kept, repeated_words, total_words = [], 0, 0
        for s in sents[p.url]:
            k = _key(s)
            words = len(s.split())
            total_words += words
            sitewide = n >= 5 and df[k] >= min_pages and df[k] / n > sitewide_share
            in_section = section_n[sec] >= 5 and section_df[sec][k] >= min_pages and section_df[sec][k] / section_n[sec] > section_share
            if (sitewide or in_section) and words >= 3:
                repeated_words += words
                if words >= MIN_SENT_WORDS:
                    pages_by_key[k].append(p.url)
            else:
                kept.append(s)
        out.main_text[p.url] = " ".join(kept)
        out.repeated_share[p.url] = repeated_words / total_words if total_words else 0.0
    # group long repeated sentences that occur on the same set of pages into passages
    groups: dict[tuple[str, ...], list[str]] = defaultdict(list)
    for k, urls in pages_by_key.items():
        groups[tuple(sorted(set(urls)))].append(k)
    for urls, keys in groups.items():
        words = sum(len(k.split()) for k in keys)
        if len(urls) >= min_pages and words >= 25:
            sample = next(s for s in sents[urls[0]] if _key(s) == keys[0])
            out.blocks.append({"pages": list(urls), "words": words, "sentences": len(keys), "sample": sample[:300]})
    out.blocks.sort(key=lambda b: -(b["words"] * len(b["pages"])))
    return out


def cached_analysis(ctx) -> BoilerplateAnalysis:
    if "boilerplate" not in ctx.shared:
        ctx.shared["boilerplate"] = analyse(ctx.site)
    return ctx.shared["boilerplate"]


def select_pages(site: SiteModel, limit: int) -> list[PageData]:
    """Most important indexable, non-utility pages first (homepage, then by inlinks and depth)."""
    home = site.homepage
    pages = [p for p in site.indexable_pages() if not is_utility(p.url)]
    pages.sort(key=lambda p: (0 if home and p.url == home.url else 1, -p.inlinks, p.depth if p.depth is not None else 99,
                              p.url))
    return pages[:limit]


def page_summary(p: PageData, main_text: str, *, excerpt_words: int = 220) -> str:
    h1 = [t for lvl, t in p.headings if lvl == 1][:2]
    outline = [f"{'  ' * (lvl - 2)}H{lvl}: {t}" for lvl, t in p.headings if lvl in (2, 3) and t][:20]
    words = main_text.split()
    excerpt = " ".join(words[:excerpt_words]) + (" …" if len(words) > excerpt_words else "")
    lines = [f"URL: {p.url}", f"Title: {p.title or '(none)'}",
             f"Meta description: {p.meta_description or '(none)'}", f"H1: {' | '.join(h1) or '(none)'}"]
    if outline:
        lines.append("Outline:\n" + "\n".join(outline))
    lines.append(f"Main text ({len(words)} words, excerpt): {excerpt or '(no unique text)'}")
    return "\n".join(lines)


def site_context(site: SiteModel, inputs) -> str:
    home = site.homepage
    bits = [f"Website: {site.facts.origin}"]
    if home and home.title:
        bits.append(f"Homepage title: {home.title}")
    for label, v in (("Business type", inputs.business_type), ("Brand", inputs.brand),
                     ("Target country", inputs.country), ("Language", inputs.language or (home.lang if home else None)),
                     ("Owner's primary keywords", ", ".join(inputs.keywords) if inputs.keywords else None)):
        if v:
            bits.append(f"{label}: {v}")
    return "\n".join(bits)


def chunks(items: list, size: int) -> list[list]:
    return [items[i:i + size] for i in range(0, len(items), size)]
