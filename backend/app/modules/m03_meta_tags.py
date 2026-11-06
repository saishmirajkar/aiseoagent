"""Module 3: Meta Tags (deterministic checks; LLM rewrites are added by the LLM layer)."""
from __future__ import annotations

import asyncio
from collections import defaultdict

from app.modules.base import (
    AuditContext, Category, CheckModule, CheckResult, CheckSpec, FindingItem, Integration, Level, Need,
)
from app.llm.client import BOOL, LLMError, STR, arr, obj
from app.llm.summaries import chunks, select_pages, site_context
from app.modules.m01_keywords import kw_tokens
from app.modules.pixel_width import title_px

H, M, L = Level.HIGH, Level.MED, Level.LOW
NO_LLM = "Anthropic API key not configured (add the ANTHROPIC_API_KEY secret)"
REWRITE_SYSTEM = """You are an SEO copywriter improving title tags and meta descriptions.
For each page you get its current title, meta description, H1, target keyword, search intent and the
problems found. Return per page:
- intent_aligned: true if the current title and description match the search intent and promise what
  the page delivers; issue: one short sentence when they don't ("" otherwise).
- titles: two alternative titles (50-60 characters, primary keyword near the start, brand at the end
  only if it fits). descriptions: two alternative meta descriptions (120-155 characters, specific benefit
  plus a reason to click). Return empty lists when the page has no problems and is aligned.
Use only facts present in the page data; write in the page's language; copy each URL exactly."""
REWRITE_SCHEMA = obj({"pages": arr(obj({"url": STR, "intent_aligned": BOOL, "issue": STR,
                                        "titles": arr(STR), "descriptions": arr(STR)}))})
TITLE_CHECKS = ("meta.title_missing", "meta.title_duplicate", "meta.title_too_long", "meta.title_too_short",
                "meta.keyword_in_title")
DESC_CHECKS = ("meta.description_missing", "meta.description_duplicate", "meta.description_length")


class MetaTags(CheckModule):
    key = "meta_tags"
    number = 3
    name = "Meta Tags"
    category = Category.ONPAGE
    needs = frozenset({Need.PAGES})
    integrations = frozenset({Integration.LLM})
    depends_on = ("keywords",)
    checks = (
        CheckSpec("meta.title_missing", "Missing title tag", H, L,
                  "The title is the main headline in search results and a strong relevance signal.",
                  "Write a unique, descriptive title (~50–60 characters) for each page.",
                  population="pages", missing_label="Title tag missing on {affected} page(s)"),
        CheckSpec("meta.title_multiple", "Multiple title tags", L, L,
                  "With several <title> tags, search engines may pick the wrong one.",
                  "Keep exactly one <title> in <head>.", population="pages"),
        CheckSpec("meta.title_duplicate", "Duplicate titles", M, M,
                  "Identical titles make pages compete with each other and hide what is unique.",
                  "Give each page a unique title that reflects its specific content.", population="pages"),
        CheckSpec("meta.title_too_long", "Title too long (truncated in results)", L, L,
                  "Titles beyond ~60 characters / ~580 px are cut off in search results.",
                  "Shorten to ~50–60 characters, putting the main keyword first.", population="pages"),
        CheckSpec("meta.title_too_short", "Title too short", L, L,
                  "Very short titles waste space that could describe the page and earn clicks.",
                  "Expand to ~50–60 characters with the main topic and a differentiator.", population="pages"),
        CheckSpec("meta.description_missing", "Missing meta description", M, L,
                  "Without one, search engines pick a snippet, which is often less compelling.",
                  "Write a unique 120–160 character description that summarises the page and invites the click.",
                  population="pages", missing_label="Meta descriptions missing on {affected} page(s)"),
        CheckSpec("meta.description_multiple", "Multiple meta descriptions", L, L,
                  "Several description tags send conflicting signals.",
                  "Keep exactly one meta description.", population="pages"),
        CheckSpec("meta.description_duplicate", "Duplicate meta descriptions", M, M,
                  "Duplicate descriptions make results look identical and reduce click-through.",
                  "Write a unique description for each page.", population="pages"),
        CheckSpec("meta.description_length", "Meta description too short or too long", L, L,
                  "Descriptions outside ~120–160 characters are truncated or look thin.",
                  "Aim for 120–160 characters.", population="pages"),
        CheckSpec("meta.social_tags", "Missing Open Graph / Twitter tags", L, L,
                  "Without og:title/og:description/og:image, shared links render poorly on social and chat apps.",
                  "Add og:title, og:description, og:image (and twitter:card).", population="pages",
                  missing_label="Open Graph tags missing on {affected} page(s)"),
        CheckSpec("meta.keyword_in_title", "Primary keyword present in title", M, L,
                  "The title should contain the page's primary keyword.",
                  "Include the primary keyword near the start of the title."),
        CheckSpec("meta.intent_alignment", "Title/description match search intent", M, M,
                  "Titles that mismatch intent get fewer clicks.",
                  "Rewrite to match what the searcher wants (learn, compare, buy, find)."),
    )

    async def run(self, ctx: AuditContext) -> list[CheckResult]:
        th = ctx.settings.thresholds
        pages = [p for p in ctx.site.html_pages() if not p.is_noindex]
        indexable = [p for p in pages if p.canonical_is_self]
        n = len(pages)
        out: list[CheckResult] = []

        out.append(self.by_population("meta.title_missing",
                                      [FindingItem(p.url, {"title": "(missing)" if p.title is None else "(empty)"}) for p in pages if not p.title], n,
                                      summary="Pages without a title"))
        out.append(self.by_population("meta.title_multiple",
                                      [FindingItem(p.url, {"title_tags": p.title_count}) for p in pages if p.title_count > 1],
                                      n, fail=False, summary="Pages with more than one <title>"))
        out.append(self._duplicates("meta.title_duplicate", indexable, lambda p: p.title, fail=True,
                                    label="title"))
        long_, short = [], []
        for p in pages:
            if not p.title:
                continue
            chars, px = len(p.title), title_px(p.title)
            if chars > th.title_max_chars or px > th.title_max_px:
                long_.append(FindingItem(p.url, {"title": p.title, "chars": chars, "pixels_est": px}))
            elif chars < th.title_min_chars:
                short.append(FindingItem(p.url, {"title": p.title, "chars": chars}))
        r = self.by_population("meta.title_too_long", long_, n, fail=False,
                               summary=f"Titles over {th.title_max_chars} chars or ~{th.title_max_px}px")
        r.sources = ["Crawl", "Estimate (pixel width, Arial 20px)"]
        out.append(r)
        out.append(self.by_population("meta.title_too_short", short, n, fail=False,
                                      summary=f"Titles under {th.title_min_chars} characters"))

        out.append(self.by_population("meta.description_missing",
                                      [FindingItem(p.url, {"meta_description": "(missing)" if p.meta_description is None else "(empty)"})
                                       for p in pages if not p.meta_description], n,
                                      summary="Pages without a meta description"))
        out.append(self.by_population("meta.description_multiple",
                                      [FindingItem(p.url, {"description_tags": p.description_count})
                                       for p in pages if p.description_count > 1], n, fail=False,
                                      summary="Pages with more than one meta description"))
        out.append(self._duplicates("meta.description_duplicate", indexable, lambda p: p.meta_description,
                                    fail=False, label="meta_description"))
        bad_len = []
        for p in pages:
            d = p.meta_description
            if d and not (th.description_min_chars <= len(d) <= th.description_max_chars):
                bad_len.append(FindingItem(p.url, {"meta_description": d, "chars": len(d),
                                                   "problem": "too short" if len(d) < th.description_min_chars else "too long"}))
        out.append(self.by_population("meta.description_length", bad_len, n, fail=False,
                                      summary=f"Descriptions outside {th.description_min_chars}–{th.description_max_chars} characters"))

        social = []
        for p in indexable:
            missing = [k for k in ("title", "description", "image") if not p.og.get(k)]
            if missing:
                social.append(FindingItem(p.url, {"missing_og": missing, "twitter_card": p.twitter.get("card")},
                                          generated_fix=self._og_fix(p, missing)))
        out.append(self.by_population("meta.social_tags", social, len(indexable), fail=False,
                                      summary="Pages missing Open Graph tags"))

        kmap = ctx.shared.get("keyword_map")
        if not kmap:
            reason = NO_LLM if ctx.llm is None else "No keyword map (Keyword Research module produced no data)"
            out.append(self.not_checked("meta.keyword_in_title", reason))
            out.append(self.not_checked("meta.intent_alignment", reason))
            return out
        missing_kw = []
        for p in pages:
            row = kmap.get(p.url)
            kw = (row or {}).get("primary_keyword")
            if not kw or not p.title:
                continue
            need, have = kw_tokens(kw), kw_tokens(p.title)
            if need and len(need & have) / len(need) < 0.6:
                missing_kw.append(FindingItem(p.url, {"title": p.title, "primary_keyword": kw,
                                                      "missing_words": sorted(need - have)}))
        r = self.by_population("meta.keyword_in_title", missing_kw, len([p for p in pages if p.url in kmap]),
                               fail=False, summary="Titles that do not contain the page's primary keyword")
        r.sources = ["Crawl", f"Keyword map (AI analysis, {ctx.settings.llm.reasoning_model})"]
        out.append(r)
        out.append(await self._rewrites(ctx, out, kmap))
        return out

    async def _rewrites(self, ctx: AuditContext, results: list[CheckResult], kmap: dict) -> CheckResult:
        """LLM pass: intent alignment for analysed pages + two rewrites for every flagged page."""
        problems: dict[str, list[str]] = defaultdict(list)
        for r in results:
            if r.status.value in ("FAIL", "WARNING") and r.spec.id in TITLE_CHECKS + DESC_CHECKS:
                for i in r.items:
                    problems[i.url].append(r.spec.title)
        pages = [p for p in select_pages(ctx.site, ctx.settings.llm.max_pages) if p.url in kmap]
        if not pages:
            return self.not_applicable("meta.intent_alignment", "No mapped pages")
        system = REWRITE_SYSTEM + "\n\nSite context:\n" + site_context(ctx.site, ctx.inputs)
        got: dict[str, dict] = {}
        errors: list[str] = []

        async def batch(group):
            parts = []
            for p in group:
                k = kmap[p.url]
                h1 = next((t for lvl, t in p.headings if lvl == 1), "")
                parts.append(f"<page>\nURL: {p.url}\nTitle: {p.title or '(none)'}\n"
                             f"Meta description: {p.meta_description or '(none)'}\nH1: {h1 or '(none)'}\n"
                             f"Target keyword: {k.get('primary_keyword')}\nIntent: {k.get('intent')}\n"
                             f"Problems: {'; '.join(sorted(set(problems.get(p.url, [])))) or 'none'}\n</page>")
            try:
                data = await ctx.llm.json(task="meta.rewrites", role="reasoning", system=system,
                                          user="\n\n".join(parts), schema=REWRITE_SCHEMA, max_tokens=6000)
            except LLMError as e:
                errors.append(str(e))
                return
            for row in data.get("pages", []):
                got[row.get("url")] = row

        await asyncio.gather(*(batch(g) for g in chunks(pages, 10)))
        if not got:
            return self.not_checked("meta.intent_alignment", f"LLM analysis failed: {errors[0]}" if errors else "No output")
        th = ctx.settings.thresholds

        def rewrite_fix(row: dict) -> dict | None:
            titles = [{"text": t, "chars": len(t), "pixels_est": title_px(t),
                       "fits": len(t) <= th.title_max_chars and title_px(t) <= th.title_max_px}
                      for t in row.get("titles", [])[:2]]
            descs = [{"text": d, "chars": len(d),
                      "fits": th.description_min_chars <= len(d) <= th.description_max_chars}
                     for d in row.get("descriptions", [])[:2]]
            if not titles and not descs:
                return None
            code = "\n".join([f"Title option {i + 1}: {t['text']}" for i, t in enumerate(titles)]
                              + [f"Description option {i + 1}: {d['text']}" for i, d in enumerate(descs)])
            return {"type": "rewrite", "titles": titles, "descriptions": descs, "code": code}

        # attach rewrites to every flagged item
        for r in results:
            if r.spec.id in TITLE_CHECKS + DESC_CHECKS:
                for i in r.items:
                    row = got.get(i.url)
                    if row and i.generated_fix is None:
                        i.generated_fix = rewrite_fix(row)
        mis = []
        for p in pages:
            row = got.get(p.url)
            if row and not row.get("intent_aligned", True):
                mis.append(FindingItem(p.url, {"title": p.title, "intent": kmap[p.url].get("intent"),
                                               "issue": row.get("issue")}, generated_fix=rewrite_fix(row)))
        note = f" ({errors[0]})" if errors else ""
        r = self.by_population("meta.intent_alignment", mis, len(got), fail=False,
                               summary=f"Titles/descriptions that don't match search intent{note}")
        r.sources = [f"AI analysis ({ctx.settings.llm.reasoning_model})"]
        return r

    def _duplicates(self, check_id: str, pages, getter, *, fail: bool, label: str) -> CheckResult:
        groups: dict[str, list[str]] = defaultdict(list)
        for p in pages:
            v = getter(p)
            if v:
                groups[v.strip().lower()].append(p.url)
        items = []
        for urls in groups.values():
            if len(urls) > 1:
                value = getter(next(p for p in pages if p.url == urls[0]))
                for u in urls:
                    items.append(FindingItem(u, {label: value, "shared_with": [x for x in urls if x != u][:10],
                                                 "group_size": len(urls)}))
        n_groups = sum(1 for g in groups.values() if len(g) > 1)
        return self.by_population(check_id, items, len([p for p in pages if getter(p)]), fail=fail,
                                  summary=f"{n_groups} group(s) of pages share the same {label.replace('_', ' ')}")

    @staticmethod
    def _og_fix(p, missing: list[str]) -> dict:
        from app.modules.common import esc_attr
        lines = []
        if "title" in missing and p.title:
            lines.append(f'<meta property="og:title" content="{esc_attr(p.title)}">')
        if "description" in missing and p.meta_description:
            lines.append(f'<meta property="og:description" content="{esc_attr(p.meta_description)}">')
        if "image" in missing:
            lines.append('<meta property="og:image" content="https://… (1200×630 image URL)">')
        lines.append(f'<meta property="og:url" content="{esc_attr(p.url)}">')
        if not p.twitter.get("card"):
            lines.append('<meta name="twitter:card" content="summary_large_image">')
        return {"type": "html", "where": "<head>", "code": "\n".join(lines)}
