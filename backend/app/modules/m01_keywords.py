"""Module 1: Keyword Research & Mapping (LLM layer).

Per page: primary/secondary/long-tail keywords, search intent and topic cluster, extracted by
the reasoning model from compact page summaries. Deterministic code then detects
cannibalization, unmapped owner keywords and builds the keyword map. Search volume and
difficulty come from the keyword engine (Phase 4) and are NOT_CHECKED until then — the
model is never asked to guess them.
"""
from __future__ import annotations

import asyncio
import re
from collections import defaultdict

from app.crawler.fetcher import Fetcher
from app.crawler.parser import parse_page
from app.crawler.rate_limit import HostRateLimiter
from app.crawler.urls import SiteScope, host_of, normalize_url
from app.config import env
from app.llm.client import BudgetExceeded, LLMError, STR, arr, enum, obj
from app.llm.summaries import cached_analysis, chunks, page_summary, select_pages, site_context
from app.modules.base import (
    AuditContext, Category, CheckModule, CheckResult, CheckSpec, FindingItem, Integration, Level, Need, Status,
)
from app.sitemodel.types import PageData

H, M, L = Level.HIGH, Level.MED, Level.LOW
INTENTS = ("informational", "navigational", "commercial", "transactional")
NO_LLM = "Anthropic API key not configured (add the ANTHROPIC_API_KEY secret)"
BATCH = 8
_W = re.compile(r"\w+", re.UNICODE)
STOP = {"the", "a", "an", "and", "or", "for", "of", "in", "to", "on", "with", "by", "at", "is", "are", "best",
        "near", "me", "your", "our", "from", "how"}

SYSTEM = """You are a senior SEO strategist doing keyword mapping for a website audit.
For each page you receive, identify what it should rank for, based only on the page's content and purpose:
- primary_keyword: the single most valuable search query this page can realistically target (2-5 words,
  lower case, in the page's language). Use "" if the page has no clear topic.
- secondary_keywords: 3-5 closely related queries the same page can also target.
- long_tail: 2-4 longer, specific queries (questions or 4+ word phrases).
- intent: the dominant search intent of the primary keyword.
- cluster: a short topic-cluster label (1-3 words) shared by pages about the same topic.
- confidence: high / medium / low.
Do not invent search volumes or difficulty. Prefer how real customers search over brand slogans.
Return exactly one entry per page, with the page's URL copied exactly."""

COMP_SYSTEM = """You compare a website's topic coverage with competitor pages for an SEO audit.
Given the site's keyword map and summaries of competitor pages, list topics and keywords the competitors
cover that the site does not. Only include gaps relevant to the site's business. For each gap give the
topic, 2-4 example keywords, which competitor URL covers it, and a suggested page or section for the site
(new page, or an existing site URL to expand). Return at most 12 gaps, most valuable first."""

PAGE_SCHEMA = obj({"pages": arr(obj({
    "url": STR, "primary_keyword": STR, "secondary_keywords": arr(STR), "long_tail": arr(STR),
    "intent": enum(*INTENTS), "cluster": STR, "confidence": enum("high", "medium", "low")}))})
GAP_SCHEMA = obj({"gaps": arr(obj({"topic": STR, "keywords": arr(STR), "competitor_url": STR,
                                   "suggestion": STR}))})


def kw_tokens(s: str) -> set[str]:
    out = set()
    for w in _W.findall((s or "").lower()):
        if w in STOP:
            continue
        out.add(w[:-1] if len(w) > 4 and w.endswith("s") else w)
    return out


def similar(a: str, b: str) -> bool:
    ta, tb = kw_tokens(a), kw_tokens(b)
    if not ta or not tb:
        return False
    return ta == tb or len(ta & tb) / len(ta | tb) >= 0.75


class KeywordResearch(CheckModule):
    key = "keywords"
    number = 1
    name = "Keyword Research & Mapping"
    category = Category.ONPAGE
    needs = frozenset({Need.PAGES})
    integrations = frozenset({Integration.LLM})
    checks = (
        CheckSpec("kw.mapping", "Keyword map (primary keyword per page)", M, M,
                  "Every important page should target one clear primary keyword that matches its intent.",
                  "Give unmapped pages a clear focus (or merge/noindex them); use the map to steer titles and H1s.",
                  population="pages"),
        CheckSpec("kw.cannibalization", "Keyword cannibalization", M, M,
                  "Several pages targeting the same query split relevance and links; often neither ranks well.",
                  "Pick one page per query: merge or differentiate the others and link them to the chosen page.",
                  population="pages"),
        CheckSpec("kw.owner_keywords", "Owner's target keywords without a matching page", M, M,
                  "Keywords you want to rank for need a page that clearly targets them.",
                  "Create or retarget a page for each unmatched keyword."),
        CheckSpec("kw.competitor_gaps", "Keyword gaps vs competitors", M, H,
                  "Topics competitors cover and you don't are traffic you cannot win today.",
                  "Plan content for the most relevant gaps (new pages or sections)."),
        CheckSpec("kw.search_volume", "Search volume for mapped keywords", M, L,
                  "Volume shows which keywords are worth prioritising.", "Prioritise by volume and intent."),
        CheckSpec("kw.difficulty", "Keyword difficulty", L, L,
                  "Difficulty shows how hard each keyword is to rank for.", "Target winnable keywords first."),
    )

    async def run(self, ctx: AuditContext) -> list[CheckResult]:
        out: list[CheckResult] = []
        engine = "Keyword engine (search volume and difficulty) is built in Phase 4"
        if ctx.llm is None:
            out = [self.not_checked(c.id, NO_LLM) for c in self.checks[:4]]
            out += [self.not_checked("kw.search_volume", engine), self.not_checked("kw.difficulty", engine)]
            return out
        pages = select_pages(ctx.site, ctx.settings.llm.max_pages)
        if not pages:
            return [self.not_applicable(c.id, "No indexable pages") for c in self.checks]
        bp = cached_analysis(ctx)
        system = SYSTEM + "\n\nSite context:\n" + site_context(ctx.site, ctx.inputs)
        kmap: dict[str, dict] = {}
        errors: list[str] = []

        async def batch(group: list[PageData]) -> None:
            user = "\n\n".join(f"<page>\n{page_summary(p, bp.main_text.get(p.url, p.text))}\n</page>" for p in group)
            try:
                data = await ctx.llm.json(task="keywords.map", role="reasoning", system=system, user=user,
                                          schema=PAGE_SCHEMA, max_tokens=6000)
            except LLMError as e:
                errors.append(str(e))
                return
            urls = {p.url for p in group}
            for row in data.get("pages", []):
                u = normalize_url(row.get("url", "")) or row.get("url")
                if u in urls:
                    row["url"] = u
                    row["secondary_keywords"] = row.get("secondary_keywords", [])[:5]
                    row["long_tail"] = row.get("long_tail", [])[:4]
                    kmap[u] = row

        sem = asyncio.Semaphore(3)

        async def guarded(g):
            async with sem:
                await batch(g)
        await asyncio.gather(*(guarded(g) for g in chunks(pages, BATCH)))
        ctx.shared["keyword_map"] = kmap
        if not kmap:
            reason = f"LLM analysis failed: {errors[0]}" if errors else "LLM returned no keyword data"
            out = [self.not_checked(c.id, reason) for c in self.checks[:4]]
            out += [self.not_checked("kw.search_volume", engine), self.not_checked("kw.difficulty", engine)]
            return out
        src = [f"AI analysis ({ctx.settings.llm.reasoning_model})"]
        ctx.shared.setdefault("metrics", []).append(("keywords", "keyword_map", list(kmap.values()), src[0]))
        partial = f" ({len(kmap)} of {len(pages)} pages analysed; {errors[0]})" if errors else ""
        skipped = len(ctx.site.indexable_pages()) - len(pages)
        cap_note = f"; {skipped} lower-priority page(s) beyond the LLM page limit were not analysed" if skipped > 0 else ""

        # mapping
        items = []
        for p in pages:
            row = kmap.get(p.url)
            if row and row.get("primary_keyword") and row.get("confidence") != "low":
                continue
            items.append(FindingItem(p.url, {"primary_keyword": (row or {}).get("primary_keyword") or "(none)",
                                             "confidence": (row or {}).get("confidence", "not analysed"),
                                             "title": p.title}))
        r = self.by_population("kw.mapping", items, len(kmap), fail=False,
                               summary=f"{len(items)} page(s) without a clear primary keyword{partial}{cap_note}",
                               pass_summary=f"All {len(kmap)} analysed pages have a clear primary keyword{cap_note}")
        r.sources = src
        out.append(r)

        # cannibalization: same/similar primary keyword with the same intent
        rows = [r for r in kmap.values() if r.get("primary_keyword")]
        groups: list[list[dict]] = []
        for row in rows:
            for g in groups:
                if similar(g[0]["primary_keyword"], row["primary_keyword"]) and g[0]["intent"] == row["intent"]:
                    g.append(row)
                    break
            else:
                groups.append([row])
        can = []
        for g in groups:
            if len(g) > 1:
                urls = [x["url"] for x in g]
                for x in g:
                    can.append(FindingItem(x["url"], {"primary_keyword": x["primary_keyword"], "intent": x["intent"],
                                                      "competing_pages": [u for u in urls if u != x["url"]][:10]}))
        n_groups = sum(1 for g in groups if len(g) > 1)
        r = self.by_population("kw.cannibalization", can, len(rows), fail=False,
                               summary=f"{n_groups} keyword(s) targeted by more than one page")
        r.sources = src + ["Deterministic comparison"]
        out.append(r)

        # owner keywords
        if not ctx.inputs.keywords:
            out.append(self.not_applicable("kw.owner_keywords", "No target keywords were provided with the audit"))
        else:
            missing = []
            for k in ctx.inputs.keywords:
                hits = [x["url"] for x in kmap.values()
                        if any(similar(k, c) or kw_tokens(k) <= kw_tokens(c)
                               for c in [x.get("primary_keyword", "")] + x.get("secondary_keywords", []))]
                if not hits:
                    missing.append(FindingItem(None, {"keyword": k}))
            status = Status.PASS if not missing else Status.WARNING
            out.append(CheckResult(spec=self.spec("kw.owner_keywords"), status=status,
                                   summary=(f"{len(missing)} of {len(ctx.inputs.keywords)} target keyword(s) have no page"
                                            if missing else "Every target keyword has a matching page"),
                                   items=missing, affected=len(missing), total=len(ctx.inputs.keywords), sources=src))

        out.append(await self._competitor_gaps(ctx, kmap, src))
        out += [self.not_checked("kw.search_volume", engine), self.not_checked("kw.difficulty", engine)]
        return out

    # ------------------------------------------------------------------ competitors
    async def _competitor_gaps(self, ctx: AuditContext, kmap: dict, src: list[str]) -> CheckResult:
        comps = ctx.inputs.competitors[:3]
        if not comps:
            return self.not_applicable("kw.competitor_gaps", "No competitor URLs were provided with the audit")
        summaries, errors = await fetch_competitors(ctx, comps)
        if not summaries:
            return self.not_checked("kw.competitor_gaps", "Could not fetch competitor pages: " + "; ".join(errors[:3]))
        site_map = "\n".join(f"- {r['url']}: {r.get('primary_keyword')} ({r.get('intent')}); "
                             f"also {', '.join(r.get('secondary_keywords', [])[:3])}" for r in kmap.values())
        user = f"<data>\nSITE KEYWORD MAP:\n{site_map}\n</data>\n\n" + "\n\n".join(
            f"<page>\n{s}\n</page>" for s in summaries)
        try:
            data = await ctx.llm.json(task="keywords.competitor_gaps", role="reasoning",
                                      system=COMP_SYSTEM + "\n\nSite context:\n" + site_context(ctx.site, ctx.inputs),
                                      user=user, schema=GAP_SCHEMA, max_tokens=4000)
        except LLMError as e:
            return self.not_checked("kw.competitor_gaps", f"LLM analysis failed: {e}")
        gaps = data.get("gaps", [])[:12]
        ctx.shared["competitor_summaries"] = summaries
        if not gaps:
            return self.result("kw.competitor_gaps", Status.PASS, "No significant topic gaps found",
                               sources=src + ["Competitor pages (crawled)"])
        items = [FindingItem(None, {"topic": g["topic"], "keywords": g["keywords"][:4],
                                    "covered_by": g["competitor_url"]},
                             generated_fix={"type": "content_plan", "note": g["suggestion"]}) for g in gaps]
        return CheckResult(spec=self.spec("kw.competitor_gaps"), status=Status.WARNING,
                           summary=f"{len(gaps)} topic gap(s) vs {len(comps)} competitor(s)", items=items,
                           affected=len(items), sources=src + ["Competitor pages (crawled)"])


async def fetch_competitors(ctx: AuditContext, comps: list[str]) -> tuple[list[str], list[str]]:
    """Fetch each competitor's homepage plus a few same-site pages it links to (politely)."""
    from app.crawler.robots import build_robots
    cfg = ctx.settings.crawl
    per = ctx.settings.llm.competitor_pages
    summaries, errors = [], []
    async with Fetcher(env().user_agent, HostRateLimiter(cfg.rate_limit_per_sec), timeout=cfg.timeout_s,
                       retries=1, max_bytes=cfg.max_html_bytes) as f:
        for url in comps:
            host = host_of(url)
            scope = SiteScope(host)
            origin = url.split("/", 3)[0] + "//" + host
            rr = await f.fetch(origin + "/robots.txt", max_bytes=256_000)
            robots = build_robots(origin + "/robots.txt", rr.status, rr.text() if rr.status == 200 else "", rr.error)
            todo, seen, got = [url], set(), 0
            while todo and got < max(1, per):
                u = todo.pop(0)
                if u in seen or not robots.can_fetch(u, cfg.user_agent_token):
                    continue
                seen.add(u)
                res = await f.fetch(u, html_only=True)
                if res.status != 200 or not res.is_html:
                    errors.append(f"{u}: {res.error or f'HTTP {res.status}'}")
                    continue
                p = PageData(url=u, final_url=res.final_url)
                parse_page(p, res.text(), scope)
                summaries.append(page_summary(p, p.text, excerpt_words=150))
                got += 1
                if u == url:
                    todo += [l.target for l in p.links if l.internal and l.target not in seen][:20]
    return summaries, errors
