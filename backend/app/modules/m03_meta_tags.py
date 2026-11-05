"""Module 3: Meta Tags (deterministic checks; LLM rewrites are added by the LLM layer)."""
from __future__ import annotations

from collections import defaultdict

from app.modules.base import (
    AuditContext, Category, CheckModule, CheckResult, CheckSpec, FindingItem, Integration, Level, Need,
)
from app.modules.pixel_width import title_px

H, M, L = Level.HIGH, Level.MED, Level.LOW
LLM_PENDING = "Needs the keyword map and LLM layer (Keyword Research module), which is not built yet"


class MetaTags(CheckModule):
    key = "meta_tags"
    number = 3
    name = "Meta Tags"
    category = Category.ONPAGE
    needs = frozenset({Need.PAGES})
    integrations = frozenset({Integration.LLM})
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

        out.append(self.not_checked("meta.keyword_in_title", LLM_PENDING))
        out.append(self.not_checked("meta.intent_alignment", LLM_PENDING))
        return out

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
