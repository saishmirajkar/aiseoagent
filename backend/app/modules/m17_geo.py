"""Module 17: GEO Content Optimization — make content easy for AI assistants to quote and trust.

Deterministic: question-style headings, FAQ sections, sources/statistics, author bylines and dates,
site-level E-E-A-T signals. LLM: answer-first openings and quotable fact sentences.
"""
from __future__ import annotations

import asyncio
import re
from urllib.parse import urlsplit

from app.llm.client import BOOL, LLMError, STR, arr, obj
from app.llm.summaries import cached_analysis, chunks, page_summary, select_pages, site_context
from app.modules.base import (
    AuditContext, Category, CheckModule, CheckResult, CheckSpec, FindingItem, Integration, Level, Need, Status,
)
from app.modules.m02_content import faq_jsonld
from app.modules.m09_schema import ARTICLE_PATH, SOCIAL

H, M, L = Level.HIGH, Level.MED, Level.LOW
NO_LLM = "Anthropic API key not configured (add the ANTHROPIC_API_KEY secret)"
QUESTION = re.compile(r"\?\s*$|^(what|how|why|when|where|which|who|can|does|do|is|are|should|will|kya|was|wie|"
                      r"warum|qu[eé]|comment|pourquoi|c[oó]mo|por qu[eé])\b", re.I)
FAQ_HEADING = re.compile(r"\b(faq|faqs|frequently asked|common questions|questions)\b", re.I)
STAT = re.compile(r"\b\d+(?:[.,]\d+)?\s?(%|percent|per cent|million|billion|km|kg|hp|cc|mph|km/h|years?)\b", re.I)
EEAT_PATHS = {"about": re.compile(r"/(about|about-us|who-we-are|company|our-story|team)(/|$)", re.I),
              "contact": re.compile(r"/(contact|contact-us|get-in-touch)(/|$)", re.I),
              "privacy": re.compile(r"/(privacy|privacy-policy|data-protection)(/|$)", re.I)}

SYSTEM = """You optimise web pages so AI assistants (ChatGPT, Claude, Perplexity, Google AI Overviews) can
quote and cite them. For each page judge:
- answer_first: true if the first ~60 words directly answer the page's main question or define the topic.
- opening_rewrite: a 40-60 word answer-first opening paragraph for the page (empty if answer_first is true).
- quotable_facts: 2-3 short, self-contained sentences stating specific facts FROM the page that an AI could
  quote verbatim (include numbers, names, specs where the page has them). Never invent facts.
- needs_sources: claims on the page that would be more credible with a cited source (up to 3; may be empty).
Write in the page's language. Copy each URL exactly."""
SCHEMA = obj({"pages": arr(obj({"url": STR, "answer_first": BOOL, "opening_rewrite": STR,
                                "quotable_facts": arr(STR), "needs_sources": arr(STR)}))})


def _is_question(h: str) -> bool:
    return bool(QUESTION.search(h.strip()))


class GeoContent(CheckModule):
    key = "geo_content"
    number = 17
    name = "GEO Content Optimization"
    category = Category.GEO
    needs = frozenset({Need.PAGES})
    integrations = frozenset({Integration.LLM})
    depends_on = ("keywords", "content")
    checks = (
        CheckSpec("geo.answer_first", "Direct answer near the top of the page", H, L,
                  "AI assistants quote pages that answer the question up front in a self-contained sentence.",
                  "Open with a 40-60 word answer/definition (see the generated rewrite), then go into detail.",
                  population="pages"),
        CheckSpec("geo.question_headings", "Question-style headings", L, L,
                  "Headings phrased as the questions people ask map directly onto AI and voice queries.",
                  "Rephrase some H2/H3 headings as the questions they answer.", population="pages"),
        CheckSpec("geo.faq", "FAQ section on key pages", M, L,
                  "Concise Q&A blocks are among the most quoted formats in AI answers.",
                  "Add a short FAQ section (AI drafts are attached where available; verify the answers).",
                  population="pages"),
        CheckSpec("geo.sources_and_stats", "Statistics and cited sources", M, M,
                  "Specific numbers with sources make content more citable and trustworthy.",
                  "Add specific figures and link to authoritative sources for key claims.", population="pages"),
        CheckSpec("geo.authorship", "Author bylines and dates on articles", M, L,
                  "Named authors and visible publish/update dates are E-E-A-T signals AI systems weigh.",
                  "Show author name (linked to a bio) and published/updated dates on every article.",
                  population="pages"),
        CheckSpec("geo.eeat_site", "Site trust signals (about, contact, policies, profiles)", M, L,
                  "About/contact pages, policies and consistent profiles help search engines and AI trust the source.",
                  "Add the missing pages/signals listed in the evidence."),
    )

    async def run(self, ctx: AuditContext) -> list[CheckResult]:
        site = ctx.site
        kmap = ctx.shared.get("keyword_map", {})
        pages = site.indexable_pages()
        if not pages:
            return [self.not_applicable(c.id, "No indexable pages") for c in self.checks]
        bp = cached_analysis(ctx)
        home = site.homepage

        def info_like(p) -> bool:
            intent = kmap.get(p.url, {}).get("intent")
            if intent:
                return intent in ("informational", "commercial")
            return bool(ARTICLE_PATH.search(urlsplit(p.url).path))

        out = [await self._answer_first(ctx, bp)]

        qh = []
        candidates = [p for p in pages if sum(1 for lvl, _ in p.headings if lvl in (2, 3)) >= 3 and info_like(p)]
        for p in candidates:
            subs = [t for lvl, t in p.headings if lvl in (2, 3) and t]
            if not any(_is_question(t) for t in subs):
                qh.append(FindingItem(p.url, {"h2_h3": subs[:6]}))
        out.append(self.by_population("geo.question_headings", qh, len(candidates), fail=False,
                                      summary="Content pages with no question-style headings"))

        key_pages = [p for p in select_pages(site, ctx.settings.llm.geo_pages or 15) if info_like(p) or (home and p.url == home.url)]
        faq_missing = []
        for p in key_pages:
            questions = sum(1 for lvl, t in p.headings if lvl in (2, 3, 4) and _is_question(t))
            has = questions >= 3 or any(FAQ_HEADING.search(t) for _, t in p.headings) or \
                "FAQPage" in p.structured_data.get("types", [])
            if not has:
                drafts = (ctx.shared.get("content_gaps") or {}).get(p.url, {}).get("faqs", [])
                fx = faq_jsonld(drafts[:5]) if drafts else None
                fix = None
                if drafts:
                    fix = {"type": "faq_draft", "faqs": [{"q": f["question"], "a": f["answer"] or "(to be written)"}
                                                        for f in drafts[:5]],
                           "code": "\n\n".join(f"Q: {f['question']}\nA: {f['answer'] or '(to be written)'}" for f in drafts[:5])}
                    if fx:
                        fix["faq_jsonld"] = fx["code"]
                faq_missing.append(FindingItem(p.url, {"question_headings": questions}, generated_fix=fix))
        out.append(self.by_population("geo.faq", faq_missing, len(key_pages), fail=False,
                                      summary="Key pages without an FAQ section"))

        weak = []
        articles = [p for p in pages if info_like(p) and len(bp.main_text.get(p.url, "").split()) >= 300]
        for p in articles:
            text = bp.main_text.get(p.url, p.text)
            stats = len(STAT.findall(text))
            refs = {urlsplit(l.target).hostname for l in p.links if not l.internal
                    and not any((urlsplit(l.target).hostname or "").endswith(d) for d in SOCIAL)}
            if stats == 0 and not refs:
                weak.append(FindingItem(p.url, {"statistics_found": stats, "external_sources": 0}))
        out.append(self.by_population("geo.sources_and_stats", weak, len(articles), fail=False,
                                      summary="Long content pages with no statistics and no cited sources"))

        auth = []
        arts = [p for p in pages if ARTICLE_PATH.search(urlsplit(p.url).path) or p.og.get("type") == "article"
                or "datePublished" in p.dates]
        for p in arts:
            missing = []
            authors = [a for a in p.authors if "://" not in a]
            if not authors:
                missing.append("author byline")
            if not any(k in p.dates for k in ("datePublished", "published_time", "time_element")):
                missing.append("machine-readable publish date")
            if not any(k in p.dates for k in ("dateModified", "modified_time")):
                missing.append("last-updated date")
            if missing:
                auth.append(FindingItem(p.url, {"missing": missing}))
        out.append(self.by_population("geo.authorship", auth, len(arts), fail=False,
                                      summary="Articles missing author or date signals"))
        out.append(self._eeat(ctx))
        return out

    def _eeat(self, ctx: AuditContext) -> CheckResult:
        site = ctx.site
        known = set(site.pages) | {l.target for p in site.html_pages() for l in p.links if l.internal}
        found = {k: sorted(u for u in known if rx.search(urlsplit(u).path))[:3] for k, rx in EEAT_PATHS.items()}
        missing = [k + " page" for k, v in found.items() if not v]
        types = {t for p in site.html_pages() for t in p.structured_data.get("types", [])}
        if not ({"Organization", "LocalBusiness"} & types):
            missing.append("Organization schema")
        home = site.homepage
        socials = [l.target for l in (home.links if home else []) if any(
            (urlsplit(l.target).hostname or "").endswith(d) for d in SOCIAL)]
        if not socials:
            missing.append("links to official social profiles")
        if home and not (home.nap.get("tel_links") or home.nap.get("text_phones") or home.nap.get("address_element")):
            missing.append("visible phone or address on the homepage")
        ev = {"found": {k: v for k, v in found.items() if v}, "missing": missing, "social_profiles": len(set(socials))}
        if not missing:
            return self.result("geo.eeat_site", Status.PASS, "Core trust signals are present", items=[FindingItem(None, ev)])
        return self.result("geo.eeat_site", Status.WARNING, "Missing: " + ", ".join(missing),
                           items=[FindingItem(None, ev)], affected=len(missing))

    async def _answer_first(self, ctx: AuditContext, bp) -> CheckResult:
        if ctx.llm is None:
            return self.not_checked("geo.answer_first", NO_LLM)
        kmap = ctx.shared.get("keyword_map", {})
        pages = [p for p in select_pages(ctx.site, ctx.settings.llm.geo_pages)
                 if len(bp.main_text.get(p.url, "").split()) >= 80]
        if not pages:
            return self.not_applicable("geo.answer_first", "No pages with enough unique text to assess")
        system = SYSTEM + "\n\nSite context:\n" + site_context(ctx.site, ctx.inputs)
        got: dict[str, dict] = {}
        errors: list[str] = []

        async def batch(group):
            user = "\n\n".join(
                f"<page>\nMain question/keyword: {kmap.get(p.url, {}).get('primary_keyword') or 'unknown'}\n"
                f"{page_summary(p, bp.main_text.get(p.url, p.text), excerpt_words=300)}\n</page>" for p in group)
            try:
                data = await ctx.llm.json(task="geo.answer_first", role="reasoning", system=system, user=user,
                                          schema=SCHEMA, max_tokens=5000)
            except LLMError as e:
                errors.append(str(e))
                return
            for row in data.get("pages", []):
                got[row.get("url")] = row

        await asyncio.gather(*(batch(g) for g in chunks(pages, 5)))
        if not got:
            return self.not_checked("geo.answer_first", f"LLM analysis failed: {errors[0]}" if errors else "No output")
        items = []
        for p in pages:
            row = got.get(p.url)
            if not row or row.get("answer_first"):
                continue
            fix = {"type": "rewrite", "code": row.get("opening_rewrite", ""),
                   "quotable_facts": row.get("quotable_facts", [])[:3], "needs_sources": row.get("needs_sources", [])[:3]}
            items.append(FindingItem(p.url, {"answer_first": False,
                                             "claims_needing_sources": row.get("needs_sources", [])[:3]},
                                     generated_fix=fix))
        note = f" ({errors[0]})" if errors else ""
        r = self.by_population("geo.answer_first", items, len(got), fail=False,
                               summary=f"Pages that don't answer their main question up front{note}")
        r.sources = [f"AI analysis ({ctx.settings.llm.reasoning_model})"]
        return r
