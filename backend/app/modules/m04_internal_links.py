"""Module 4: Internal Linking — orphans, weak/deep pages, anchor quality, link suggestions."""
from __future__ import annotations

import re
from collections import defaultdict
from urllib.parse import urlsplit

from app.modules.base import (
    AuditContext, Category, CheckModule, CheckResult, CheckSpec, FindingItem, Level, Need, Status,
)
from app.modules.common import is_utility
from app.llm.client import BOOL, INT, LLMError, STR, arr, obj
from app.llm.manual import AwaitingAnswer
from app.llm.summaries import chunks
from app.modules.textsim import TfIdf
from app.sitemodel.types import PageData

H, M, L = Level.HIGH, Level.MED, Level.LOW
GENERIC = {"click here", "here", "read more", "learn more", "more", "more info", "more information", "this",
           "this page", "link", "continue", "continue reading", "details", "view more", "see more", "go",
           "know more", "find out more", "view", "click", "visit", "read", "explore", "discover more",
           "see details", "view details", "lees meer", "mehr", "plus", "leer más"}
_TRIM = re.compile(r"^[\W_]+|[\W_]+$", re.UNICODE)
TITLE_SPLIT = re.compile(r"\s+[|–—\-:·•]\s+")
MAX_SUGGESTIONS_PER_TARGET = 3
REVIEW_SYSTEM = """You review proposed internal links for a website SEO audit. For each numbered proposal
decide whether a reader of the FROM page would genuinely benefit from a link to the TO page (keep=true) or
whether the pages are unrelated/weakly related (keep=false). For kept links write a natural, descriptive
anchor text of 2-6 words in the page language that names the TO page's topic (no "click here"), and a
short reason. Return one entry per proposal id."""
REVIEW_SCHEMA = obj({"links": arr(obj({"id": INT, "keep": BOOL, "anchor": STR, "reason": STR}))})
MIN_SIMILARITY = 0.12
MIN_SHARED_TOPICS = 2
NEAR_DUPLICATE_SIM = 0.9  # near-identical pages: a duplicate-content issue, not a linking opportunity


def norm_anchor(a: str) -> str:
    return _TRIM.sub("", (a or "").lower().strip())


def page_label(p: PageData) -> str:
    h1 = next((t for lvl, t in p.headings if lvl == 1 and t), None)
    label = h1 or (TITLE_SPLIT.split(p.title)[0] if p.title else "") or urlsplit(p.url).path.strip("/").split("/")[-1]
    return re.sub(r"\s+", " ", label).strip()[:70]


def _group_links(pairs: list[tuple[str, str, str]]) -> list[FindingItem]:
    """(source, anchor, target) -> one item per (anchor, target) pattern, listing all source pages."""
    groups: dict[tuple[str, str], list[str]] = defaultdict(list)
    for src, anchor, target in pairs:
        if src not in groups[(anchor, target)]:
            groups[(anchor, target)].append(src)
    items = [FindingItem(srcs[0], {"anchor": a or "(no text)", "target": t, "pages": len(srcs)}, also_on=srcs[1:])
             for (a, t), srcs in groups.items()]
    items.sort(key=lambda i: -i.evidence["pages"])
    return items


class InternalLinking(CheckModule):
    key = "internal_links"
    number = 4
    name = "Internal Linking"
    category = Category.ONPAGE
    needs = frozenset({Need.PAGES, Need.LINKS, Need.SITEMAP})
    checks = (
        CheckSpec("links.orphans", "Orphan pages (in sitemap, no internal links)", M, L,
                  "Pages no other page links to get little crawl priority and no link equity.",
                  "Link to each orphan from relevant pages (see the link suggestions), or remove it from the "
                  "sitemap if it should not exist.", population="pages",
                  missing_label="Internal links missing to {affected} orphan page(s)"),
        CheckSpec("links.few_inlinks", "Pages with fewer than 3 internal links pointing to them", M, L,
                  "Weakly linked pages are seen as less important and are crawled less often.",
                  "Add contextual links from related pages and hub/category pages.", population="pages"),
        CheckSpec("links.deep_pages", "Pages more than 3 clicks from the homepage", M, M,
                  "Deep pages are crawled less and receive less authority.",
                  "Link to them from the homepage, navigation, or hub pages so they are within 3 clicks.",
                  population="pages"),
        CheckSpec("links.generic_anchors", "Generic anchor text (\"click here\", \"read more\")", L, L,
                  "Anchor text tells search engines what the target page is about; generic text wastes that.",
                  "Use descriptive anchors that name the target topic.", population="links"),
        CheckSpec("links.empty_anchors", "Links with no anchor text", L, L,
                  "Links without any text (or aria-label) give no context to search engines or screen readers.",
                  "Add visible text or an aria-label describing the target.", population="links"),
        CheckSpec("links.nofollow_internal", "Internal links marked nofollow", L, L,
                  "nofollow on internal links stops link equity flowing to your own pages.",
                  "Remove rel=\"nofollow\" from internal links (use noindex on the target if needed).",
                  population="links"),
        CheckSpec("links.suggestions", "Internal link opportunities", M, L,
                  "Specific contextual links that would strengthen weakly linked pages and topic clusters.",
                  "Add the suggested links (source page → anchor → target page)."),
    )

    async def run(self, ctx: AuditContext) -> list[CheckResult]:
        site = ctx.site
        th = ctx.settings.thresholds
        ok = site.html_pages()
        home = site.homepage
        indexable = [p for p in site.indexable_pages() if not (home and p.url == home.url)]
        if not ok:
            return [self.not_applicable(c.id, "No HTML pages crawled") for c in self.checks]
        complete = site.facts.crawl.get("link_crawl_complete")
        truncated = bool(site.facts.crawl.get("limit_reached")) and not complete
        caveat = (" (the crawl hit its page limit before following every link, so some of these may be "
                  "linked from uncrawled pages; raise the page limit to confirm)") if truncated else ""
        out: list[CheckResult] = []

        # inbound internal links by target, over all crawled pages
        linked: set[str] = set()
        for p in ok:
            for l in p.links:
                if l.internal and l.target != p.url:
                    linked.add(l.target)
        sm = site.sitemap_urls
        orphans = []
        for u in sm:
            if u in linked or (home and u == home.url) or is_utility(u):
                continue
            p = site.pages.get(u)
            if p is not None and not p.indexable:
                continue
            st = site.status_of(u)
            if p is None and (st is None or st.status != 200 or st.redirects):
                continue  # only confirmed live pages
            orphans.append(FindingItem(u, {"in_sitemap": True, "crawled": p is not None}))
        if not sm:
            out.append(self.not_applicable("links.orphans", "No sitemap to compare against"))
        else:
            r = self.by_population("links.orphans", orphans, len(sm), fail=not truncated,
                                   summary=f"{len(orphans)} sitemap page(s) with no internal links{caveat}")
            if orphans and complete:
                r.summary += (" — every page reachable by following links from the homepage was crawled, "
                              "so these are only discoverable through the sitemap. Check listing pages "
                              "(e.g. a blog index) use real <a href> links, not JavaScript click handlers.")
            out.append(r)

        orphan_urls = {i.url for i in orphans}
        few = [FindingItem(p.url, {"inlinks": p.inlinks, "from": p.inlink_sources[:5]})
               for p in indexable if 0 < p.inlinks < th.important_page_min_inlinks and p.url not in orphan_urls]
        out.append(self.by_population("links.few_inlinks", few, len(indexable), fail=False,
                                      summary=f"Pages with 1–{th.important_page_min_inlinks - 1} inbound internal "
                                              f"link(s){caveat}"))
        deep = [FindingItem(p.url, {"click_depth": p.depth}) for p in indexable if p.depth is not None and p.depth > 3]
        out.append(self.by_population("links.deep_pages", sorted(deep, key=lambda i: -i.evidence["click_depth"]),
                                      len(indexable), fail=False, summary="Pages more than 3 clicks deep"))

        generic, empty, nofollow, n_internal = [], [], [], 0
        for p in ok:
            for l in p.links:
                if not l.internal:
                    continue
                n_internal += 1
                a = norm_anchor(l.anchor)
                if a in GENERIC:
                    generic.append((p.url, l.anchor.strip(), l.target))
                elif not a and not l.image_link:
                    empty.append((p.url, "", l.target))
                if l.nofollow:
                    nofollow.append((p.url, l.anchor.strip(), l.target))
        for cid, pairs, summ in (("links.generic_anchors", generic, "Internal links with generic anchor text"),
                                 ("links.empty_anchors", empty, "Internal links with no text"),
                                 ("links.nofollow_internal", nofollow, "Internal links with rel=nofollow")):
            items = _group_links(pairs)
            r = self.by_population(cid, items, n_internal, fail=False,
                                   summary=f"{summ}: {len(pairs)} link(s), {len(items)} distinct pattern(s)")
            r.affected = len(pairs)
            out.append(r)

        out.append(await self._suggestions(ctx, indexable, orphan_urls))
        return out

    # ------------------------------------------------------------------ suggestions
    async def _suggestions(self, ctx: AuditContext, indexable: list[PageData], orphan_urls: set[str]) -> CheckResult:
        site = ctx.site
        th = ctx.settings.thresholds
        sources = [p for p in site.indexable_pages() if p.text]
        if len(sources) < 3:
            return self.not_applicable("links.suggestions", "Too few pages to suggest internal links")
        tfidf = TfIdf({p.url: f"{p.title or ''} {' '.join(t for _, t in p.headings)} {p.text[:20000]}"
                       for p in sources}, groups={p.url: p.template_key or "" for p in sources})
        links_from: dict[str, set[str]] = {p.url: {l.target for l in p.links if l.internal} for p in sources}
        targets = [p for p in indexable if p.url in orphan_urls or p.inlinks < th.important_page_min_inlinks
                   or (p.depth or 0) > 3]
        targets += [site.pages[u] for u in orphan_urls if u in site.pages and site.pages[u] not in targets]
        items: list[FindingItem] = []
        for t in targets:
            cands = []
            for s in sources:
                if s.url == t.url or t.url in links_from.get(s.url, ()):
                    continue
                sim = tfidf.similarity(s.url, t.url)
                if MIN_SIMILARITY <= sim < NEAR_DUPLICATE_SIM:
                    cands.append((sim / (1 + 0.15 * (s.depth or 0)), sim, s))
            target_terms = tfidf.top_terms(t.url, 12)
            kept = 0
            for score, sim, s in sorted(cands, key=lambda c: -c[0]):
                shared = [w for w in target_terms if w in tfidf.vec.get(s.url, {})][:5]
                if len(shared) < MIN_SHARED_TOPICS:
                    continue  # one common word is not a topical relationship
                kept += 1
                if kept > MAX_SUGGESTIONS_PER_TARGET:
                    break
                items.append(FindingItem(s.url, {"kind": "contextual", "add_link_to": t.url, "anchor": page_label(t),
                                                 "similarity": round(sim, 2), "shared_topics": shared,
                                                 "target_inlinks": t.inlinks},
                                         generated_fix={"type": "internal_link", "source": s.url, "target": t.url,
                                                        "anchor": page_label(t),
                                                        "code": f'<a href="{t.url}">{page_label(t)}</a>'}))
        items += self._clusters(site, sources, links_from)
        sources_used = ["Crawl", "Text similarity (TF-IDF)"]
        note = "anchors are drafts"
        if items and ctx.llm is not None:
            items, reviewed, err = await self._review(ctx, items)
            if reviewed:
                sources_used.append(f"AI review ({ctx.settings.llm.reasoning_model})")
                note = f"{reviewed} suggestion(s) reviewed by AI for relevance and anchor text"
            elif err:
                note = f"anchors are drafts; AI review failed: {err}"
        if not items:
            return self.result("links.suggestions", Status.PASS, "No obvious internal link gaps found")
        n_targets = len({i.evidence["add_link_to"] for i in items})
        return CheckResult(spec=self.spec("links.suggestions"), status=Status.WARNING,
                           summary=f"{len(items)} suggested link(s) to {n_targets} page(s) ({note})",
                           items=items, affected=len(items), sources=sources_used)

    async def _review(self, ctx: AuditContext, items: list[FindingItem]) -> tuple[list[FindingItem], int, str | None]:
        """LLM keeps relevant suggestions and writes natural 2-6 word anchors; unreviewed ones are kept as-is."""
        site = ctx.site
        cap = ctx.settings.llm.link_suggestions_reviewed
        order = sorted(range(len(items)), key=lambda k: -(items[k].evidence.get("similarity") or 0.5))
        to_review = order[:cap]
        decisions: dict[int, dict] = {}
        err: str | None = None
        for group in chunks(to_review, 20):
            lines = []
            for k in group:
                it = items[k]
                s, t = site.pages.get(it.url), site.pages.get(it.evidence["add_link_to"])
                lines.append(f"{k}. FROM \"{page_label(s) if s else it.url}\" ({it.url}) TO \"{page_label(t) if t else ''}\" "
                             f"({it.evidence['add_link_to']}); kind: {it.evidence['kind']}; "
                             f"shared topics: {', '.join(it.evidence.get('shared_topics', [])) or 'n/a'}")
            try:
                data = await ctx.llm.json(task="links.review", role="reasoning", system=REVIEW_SYSTEM,
                                          user="<data>\n" + "\n".join(lines) + "\n</data>",
                                          schema=REVIEW_SCHEMA, max_tokens=4000)
            except LLMError as e:
                err = str(e)
                if isinstance(e, AwaitingAnswer):
                    continue  # manual AI mode: queue every batch in one run
                break
            for row in data.get("links", []):
                if row.get("id") in group:
                    decisions[row["id"]] = row
        kept = []
        for k, it in enumerate(items):
            d = decisions.get(k)
            if d is None:
                kept.append(it)
                continue
            if not d.get("keep"):
                continue
            anchor = (d.get("anchor") or it.evidence["anchor"]).strip()[:70]
            it.evidence["anchor"] = anchor
            it.evidence["why"] = d.get("reason")
            if it.generated_fix:
                it.generated_fix["anchor"] = anchor
                it.generated_fix["code"] = f'<a href="{it.evidence["add_link_to"]}">{anchor}</a>'
            kept.append(it)
        return kept, len(decisions), err

    @staticmethod
    def _clusters(site, sources: list[PageData], links_from: dict[str, set[str]]) -> list[FindingItem]:
        """Pillar ↔ cluster links within URL sections such as /blog/* or /products/*."""
        sections: dict[str, list[PageData]] = defaultdict(list)
        for p in sources:
            segs = [s for s in urlsplit(p.url).path.split("/") if s]
            if segs:
                sections[segs[0]].append(p)
        items = []
        origin = site.facts.origin
        for sec, pages in sections.items():
            # sorted so the proposals (and the cached AI review of them) don't change with crawl order
            members = sorted((p for p in pages if len([s for s in urlsplit(p.url).path.split("/") if s]) >= 2),
                             key=lambda p: p.url)
            if len(members) < 3:
                continue
            pillar = next((site.pages[u] for u in (f"{origin}/{sec}", f"{origin}/{sec}/")
                           if u in site.pages and site.pages[u].indexable), None)
            if pillar is None:
                continue
            missing_up = [m for m in members if pillar.url not in links_from.get(m.url, set())]
            missing_down = [m for m in members if m.url not in links_from.get(pillar.url, set())]
            for m in missing_up[:20]:
                items.append(FindingItem(m.url, {"kind": "cluster → pillar", "add_link_to": pillar.url,
                                                 "anchor": page_label(pillar), "section": f"/{sec}/"},
                                         generated_fix={"type": "internal_link", "source": m.url, "target": pillar.url,
                                                        "anchor": page_label(pillar),
                                                        "code": f'<a href="{pillar.url}">{page_label(pillar)}</a>'}))
            for m in missing_down[:20]:
                items.append(FindingItem(pillar.url, {"kind": "pillar → cluster", "add_link_to": m.url,
                                                      "anchor": page_label(m), "section": f"/{sec}/"},
                                         generated_fix={"type": "internal_link", "source": pillar.url, "target": m.url,
                                                        "anchor": page_label(m),
                                                        "code": f'<a href="{m.url}">{page_label(m)}</a>'}))
        return items
