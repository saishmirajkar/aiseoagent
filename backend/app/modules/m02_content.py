"""Module 2: Content Optimization — deterministic quality checks + LLM content gaps and FAQs."""
from __future__ import annotations

import asyncio
import json
import re
from statistics import median
from urllib.parse import urlsplit

from app.llm.client import BOOL, LLMError, STR, arr, obj
from app.llm.summaries import cached_analysis, chunks, page_summary, select_pages, site_context
from app.modules.base import (
    AuditContext, Category, CheckModule, CheckResult, CheckSpec, FindingItem, Integration, Level, Need, Status,
)
from app.modules.common import is_utility
from app.modules.schema_rules import is_valid_jsonld

H, M, L = Level.HIGH, Level.MED, Level.LOW
NO_LLM = "Anthropic API key not configured (add the ANTHROPIC_API_KEY secret)"
LIGHT_PAGES = re.compile(r"/(contact|contact-us|privacy|privacy-policy|terms|cookies?|legal|imprint|impressum|"
                         r"disclaimer|sitemap|login|register|faq)(/|$)", re.I)
_VOWELS = re.compile(r"[aeiouy]+")

SYSTEM = """You are a senior SEO content strategist reviewing pages of a website.
For each page you get its summary, its target keyword and the search intent. Compare what the page covers
with what a searcher with that intent expects (and with competitor pages, when provided). Return per page:
- covers_intent: true if the page already satisfies the intent well.
- missing_subtopics: up to 5 subtopics a strong page on this topic would cover but this page does not.
- missing_entities: up to 6 specific entities (products, specs, places, standards, people) worth mentioning.
- unanswered_questions: up to 5 questions searchers ask that the page does not answer.
- additions: up to 4 concrete content additions, each a suggested heading and what to write under it.
- faqs: 3-5 FAQ entries for the page. Draft each answer ONLY from facts in the page content; if the page
  does not contain the answer, set answer to "" (the site owner will write it). Never invent prices, specs,
  dates, guarantees or claims.
Write in the page's language. Be specific to this business; avoid generic advice. Copy each URL exactly."""

GAP_SCHEMA = obj({"pages": arr(obj({
    "url": STR, "covers_intent": BOOL, "missing_subtopics": arr(STR), "missing_entities": arr(STR),
    "unanswered_questions": arr(STR),
    "additions": arr(obj({"heading": STR, "what_to_write": STR})),
    "faqs": arr(obj({"question": STR, "answer": STR}))}))})


def syllables(word: str) -> int:
    w = word.lower().strip(".,;:!?\"'()")
    if not w:
        return 0
    n = len(_VOWELS.findall(w))
    if w.endswith("e") and n > 1 and not w.endswith(("le", "ee")):
        n -= 1
    return max(1, n)


def flesch(text: str) -> float | None:
    sents = [s for s in re.split(r"[.!?]+\s", text) if len(s.split()) >= 3]
    words = [w for w in re.findall(r"[A-Za-z']+", text)]
    if len(sents) < 3 or len(words) < 100:
        return None
    syl = sum(syllables(w) for w in words)
    return round(206.835 - 1.015 * (len(words) / len(sents)) - 84.6 * (syl / len(words)), 1)


def faq_jsonld(faqs: list[dict]) -> dict | None:
    answered = [f for f in faqs if f.get("question") and f.get("answer")]
    if len(answered) < 2:
        return None
    obj_ = {"@context": "https://schema.org", "@type": "FAQPage", "mainEntity": [
        {"@type": "Question", "name": f["question"], "acceptedAnswer": {"@type": "Answer", "text": f["answer"]}}
        for f in answered]}
    ok, problems = is_valid_jsonld(obj_)
    return {"type": "jsonld", "schema_type": "FAQPage", "validated": ok, "validation_problems": problems,
            "code": '<script type="application/ld+json">\n' + json.dumps(obj_, indent=2, ensure_ascii=False)
                    + "\n</script>",
            "notes": ["Publish these FAQs visibly on the page before adding the markup; answers are drafts "
                      "taken from the page — verify them. Google shows FAQ rich results only for some sites, "
                      "but the Q&A format helps AI assistants quote the page."]}


class ContentOptimization(CheckModule):
    key = "content"
    number = 2
    name = "Content Optimization"
    category = Category.ONPAGE
    needs = frozenset({Need.PAGES})
    integrations = frozenset({Integration.LLM})
    depends_on = ("keywords",)
    checks = (
        CheckSpec("content.thin", "Thin content", M, M,
                  "Pages with little unique text rarely rank and can drag down perceived site quality.",
                  "Expand with useful, specific content, or merge/noindex pages that cannot be improved.",
                  population="pages", missing_label="Thin content on {affected} page(s)"),
        CheckSpec("content.duplicates", "Duplicate or near-duplicate pages", M, M,
                  "Duplicate pages compete with each other; search engines pick one and may ignore the rest.",
                  "Merge duplicates, or set a canonical to the preferred page and differentiate the others.",
                  population="pages"),
        CheckSpec("content.repeated_blocks", "Large text blocks repeated across pages", H, M,
                  "When most of a page is the same text as other pages, it has little unique value; leftover "
                  "template or placeholder text is a common cause.",
                  "Remove placeholder/template text and replace it with content specific to each page.",
                  population="pages"),
        CheckSpec("content.h1", "Missing or multiple H1 headings", M, L,
                  "The H1 states the page topic for users and search engines; there should be exactly one.",
                  "Give each page exactly one descriptive H1 that includes its main topic.", population="pages",
                  missing_label="H1 heading missing on {affected} page(s)"),
        CheckSpec("content.heading_order", "Skipped heading levels", L, L,
                  "Skipping levels (H2 → H4) weakens the document outline for search engines and screen readers.",
                  "Use headings in order (H1 → H2 → H3) and don't pick levels for styling.", population="pages"),
        CheckSpec("content.readability", "Hard-to-read content (Flesch Reading Ease < 30)", L, M,
                  "Very dense text loses readers and is harder for AI systems to quote.",
                  "Shorten sentences, use plain words, add lists and subheadings.", population="pages"),
        CheckSpec("content.gaps", "Content gaps vs search intent", H, M,
                  "Pages that miss subtopics, entities and questions searchers expect lose to more complete pages.",
                  "Add the suggested sections and FAQs (see the generated content brief).", population="pages"),
    )

    async def run(self, ctx: AuditContext) -> list[CheckResult]:
        site = ctx.site
        th = ctx.settings.thresholds
        bp = cached_analysis(ctx)
        pages = [p for p in site.indexable_pages()]
        n = len(pages)
        if not n:
            return [self.not_applicable(c.id, "No indexable pages") for c in self.checks]
        out: list[CheckResult] = []

        thin = []
        for p in pages:
            if is_utility(p.url) or LIGHT_PAGES.search(urlsplit(p.url).path) or (site.homepage and p.url == site.homepage.url):
                continue
            words = len(bp.main_text.get(p.url, p.text).split())
            if words < th.thin_content_words:
                thin.append(FindingItem(p.url, {"unique_words": words, "total_words": p.word_count}))
        out.append(self.by_population("content.thin", sorted(thin, key=lambda i: i.evidence["unique_words"]), n,
                                      fail=False, summary=f"Pages with under {th.thin_content_words} words of "
                                                          "unique main text (navigation/footer/repeated text excluded)"))

        dup = []
        indexable = {p.url for p in pages}
        for g in site.duplicate_groups:
            g = [u for u in g if u in indexable]
            if len(g) < 2:
                continue
            keep = max(g, key=lambda u: (site.pages[u].inlinks, -(site.pages[u].depth or 99)))
            for u in g:
                if u != keep:
                    dup.append(FindingItem(u, {"duplicate_of": keep, "group_size": len(g)},
                                           generated_fix={"type": "html", "where": "<head>",
                                                          "code": f'<link rel="canonical" href="{keep}">'}))
        out.append(self.by_population("content.duplicates", dup, n, summary="Pages duplicating another page's content"))

        blocks = []
        worst = 0.0
        for b in bp.blocks:
            totals = [site.pages[u].word_count for u in b["pages"] if u in site.pages and site.pages[u].word_count]
            share = b["words"] / median(totals) if totals else 0
            if share >= 0.3 or b["words"] >= 150:
                worst = max(worst, share)
                blocks.append(FindingItem(b["pages"][0], {"repeated_words": b["words"], "pages": len(b["pages"]),
                                                          "share_of_page": f"{min(share, 1):.0%}", "sample": b["sample"]},
                                          also_on=b["pages"][1:]))
        r = self.by_population("content.repeated_blocks", blocks, n, fail=worst >= 0.5,
                               summary=f"{len(blocks)} repeated passage(s) making up a large part of the pages they appear on")
        if blocks:
            r.affected = len({u for i in blocks for u in [i.url, *i.also_on]})
        out.append(r)

        h1 = []
        for p in pages:
            n_h1 = sum(1 for lvl, t in p.headings if lvl == 1)
            if n_h1 == 0:
                h1.append(FindingItem(p.url, {"h1_count": 0, "problem": "missing"}))
            elif n_h1 > 1:
                h1.append(FindingItem(p.url, {"h1_count": n_h1, "h1": [t for lvl, t in p.headings if lvl == 1][:5],
                                              "problem": "multiple"}))
        out.append(self.by_population("content.h1", h1, n, fail=any(i.evidence["problem"] == "missing" for i in h1),
                                      summary="Pages without exactly one H1"))

        skips = []
        for p in pages:
            prev, bad = 0, []
            for lvl, t in p.headings:
                if prev and lvl > prev + 1:
                    bad.append(f"H{prev}→H{lvl} ({t[:40]})")
                prev = lvl
            if bad:
                skips.append(FindingItem(p.url, {"skips": bad[:5]}))
        out.append(self.by_population("content.heading_order", skips, n, fail=False, summary="Pages that skip heading levels"))

        scored, hard = 0, []
        for p in pages:
            if p.lang and not p.lang.lower().startswith("en"):
                continue
            score = flesch(bp.main_text.get(p.url, p.text))
            if score is None:
                continue
            scored += 1
            if score < 30:
                hard.append(FindingItem(p.url, {"flesch_reading_ease": score}))
        if not scored:
            out.append(self.not_applicable("content.readability",
                                           "No English pages with enough text (the readability formula is English-only)"))
        else:
            r = self.by_population("content.readability", hard, scored, fail=False,
                                   summary="Pages with very difficult text (Flesch < 30)")
            r.sources = ["Flesch Reading Ease (computed on main text)"]
            out.append(r)

        out.append(await self._gaps(ctx, bp))
        return out

    async def _gaps(self, ctx: AuditContext, bp) -> CheckResult:
        if ctx.llm is None:
            return self.not_checked("content.gaps", NO_LLM)
        kmap = ctx.shared.get("keyword_map", {})
        pages = select_pages(ctx.site, ctx.settings.llm.content_gap_pages)
        if not pages:
            return self.not_applicable("content.gaps", "No pages to analyse")
        comp = ctx.shared.get("competitor_summaries") or []
        system = SYSTEM + "\n\nSite context:\n" + site_context(ctx.site, ctx.inputs)
        if comp:
            system += "\n\nCompetitor pages for comparison:\n" + "\n\n".join(f"<data>\n{c}\n</data>" for c in comp[:8])
        results: dict[str, dict] = {}
        errors: list[str] = []

        async def batch(group):
            parts = []
            for p in group:
                k = kmap.get(p.url, {})
                parts.append(f"<page>\nTarget keyword: {k.get('primary_keyword') or 'unknown'}\n"
                             f"Search intent: {k.get('intent') or 'unknown'}\n"
                             f"{page_summary(p, bp.main_text.get(p.url, p.text), excerpt_words=350)}\n</page>")
            try:
                data = await ctx.llm.json(task="content.gaps", role="reasoning", system=system,
                                          user="\n\n".join(parts), schema=GAP_SCHEMA, max_tokens=8000)
            except LLMError as e:
                errors.append(str(e))
                return
            want = {p.url for p in group}
            for row in data.get("pages", []):
                if row.get("url") in want:
                    results[row["url"]] = row

        sem = asyncio.Semaphore(3)

        async def guarded(g):
            async with sem:
                await batch(g)
        await asyncio.gather(*(guarded(g) for g in chunks(pages, 4)))
        if not results:
            return self.not_checked("content.gaps", f"LLM analysis failed: {errors[0]}" if errors else "No LLM output")
        ctx.shared["content_gaps"] = results  # FAQ drafts are reused by the GEO module
        items = []
        for p in pages:
            row = results.get(p.url)
            if not row:
                continue
            if row["covers_intent"]:
                continue  # optional ideas for pages that already satisfy the intent are not issues
            brief = {"type": "content_brief",
                     "add_sections": [f"{a['heading']}: {a['what_to_write']}" for a in row["additions"][:4]],
                     "faqs": [{"q": f["question"], "a": f["answer"] or "(to be written by the site owner)"}
                              for f in row["faqs"][:5]],
                     "code": "\n".join([f"## {a['heading']}\n{a['what_to_write']}" for a in row["additions"][:4]])}
            fx = faq_jsonld(row["faqs"][:5])
            if fx:
                brief["faq_jsonld"] = fx["code"]
                brief["faq_jsonld_validated"] = fx["validated"]
            items.append(FindingItem(p.url, {"covers_intent": row["covers_intent"],
                                             "missing_subtopics": row["missing_subtopics"][:5],
                                             "missing_entities": row["missing_entities"][:6],
                                             "unanswered_questions": row["unanswered_questions"][:5]},
                                     generated_fix=brief))
        note = f" ({errors[0]})" if errors else ""
        r = self.by_population("content.gaps", items, len(results), fail=False,
                               summary=f"{len(items)} of {len(results)} analysed page(s) miss important subtopics "
                                       f"or questions{note}",
                               pass_summary=f"All {len(results)} analysed pages cover their search intent")
        r.sources = [f"AI analysis ({ctx.settings.llm.reasoning_model})"] + (["Competitor pages (crawled)"] if ctx.shared.get("competitor_summaries") else [])
        return r
