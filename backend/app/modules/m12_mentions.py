"""Module 12: Brand Mentions & Reputation — unlinked mentions, share of mentions vs competitors,
and review sentiment from publicly available snippets."""
from __future__ import annotations

from app.crawler.urls import host_of
from app.datalayer.backlinks import registered
from app.llm.client import LLMError, STR, arr, enum, obj
from app.modules.base import (
    AuditContext, Category, CheckModule, CheckResult, CheckSpec, FindingItem, Integration, Level, Need, Status,
)
from app.modules.m10_backlinks import NO_SEARCH, brand_of
from app.providers.search import SearchError

H, M, L = Level.HIGH, Level.MED, Level.LOW
REVIEW_SYSTEM = """You summarise publicly visible review snippets about a business for an SEO audit.
Classify overall sentiment, list recurring positive and negative themes (only those supported by more
than one snippet when possible), and note which sources the snippets come from. If the snippets are
not actually reviews of this business, say so in the summary and use sentiment "unknown"."""
REVIEW_SCHEMA = obj({"sentiment": enum("positive", "mixed", "negative", "unknown"), "summary": STR,
                     "positive_themes": arr(STR), "negative_themes": arr(STR)})


def competitor_brand(url: str) -> str:
    dom = registered(host_of(url))
    return dom.split(".")[0]


class BrandMentions(CheckModule):
    key = "brand_mentions"
    number = 12
    name = "Brand Mentions & Reputation"
    category = Category.OFFPAGE
    needs = frozenset({Need.SITE_FACTS})
    integrations = frozenset({Integration.SEARCH, Integration.LLM})
    depends_on = ("backlinks",)
    checks = (
        CheckSpec("mentions.unlinked", "Unlinked brand mentions", M, L,
                  "Pages that already mention you are the easiest links to win — ask them to add one.",
                  "Contact each site and ask for the mention to link to your site (drafts in Outreach)."),
        CheckSpec("mentions.share", "Brand visibility vs competitors", M, H,
                  "How often your brand appears on the web compared with competitors, from the same queries.",
                  "Grow mentions through PR, partnerships, reviews and community participation."),
        CheckSpec("mentions.reviews", "Public review sentiment", M, M,
                  "Review sentiment influences clicks, conversions and how AI assistants describe you.",
                  "Address recurring complaints and ask happy customers for reviews."),
    )

    async def run(self, ctx: AuditContext) -> list[CheckResult]:
        brand = brand_of(ctx)
        out: list[CheckResult] = []
        disc = ctx.shared.get("discovery")
        if ctx.search is None:
            return [self.not_checked(c.id, NO_SEARCH) for c in self.checks]
        if not brand:
            return [self.not_applicable(c.id, "No brand name provided or detected") for c in self.checks]

        if disc is None or not disc.queries:
            out.append(self.not_checked("mentions.unlinked", "Backlink discovery did not run"))
        else:
            items = [FindingItem(m.source_url, {"title": m.title, "snippet": m.snippet,
                                                "source_authority": m.authority if m.authority is not None else "low/unknown"})
                     for m in sorted(disc.mentions, key=lambda m: -(m.authority or 0))]
            ctx.shared["unlinked_mentions"] = [i.url for i in items]
            out.append(CheckResult(spec=self.spec("mentions.unlinked"),
                                   status=Status.WARNING if items else Status.PASS,
                                   summary=(f"{len(items)} page(s) mention the brand without linking (sample of "
                                            f"{disc.pages_checked} checked pages)" if items
                                            else f"No unlinked mentions among {disc.pages_checked} checked pages"),
                                   items=items, affected=len(items), sources=["Brave Search", "Pages fetched"]))

        # share of mentions: number of off-site results for each brand's exact-name query
        site_domain = registered(ctx.site.facts.host)
        brands = [(brand, site_domain)] + [(competitor_brand(c), registered(host_of(c))) for c in ctx.inputs.competitors[:3]]
        counts, err = {}, None
        for name, dom in brands:
            try:
                resp = await ctx.search.search(f'"{name}" -site:{dom}', count=20, country=ctx.inputs.country)
            except SearchError as e:
                err = str(e)
                break
            counts[name] = len({registered(host_of(r.url)) for r in resp.results if registered(host_of(r.url)) != dom})
        if brand not in counts:
            out.append(self.not_checked("mentions.share", err or "Search failed"))
        else:
            items = [FindingItem(None, {"brand": n, "domains_mentioning (top 20 results)": c,
                                        "is_you": n == brand}) for n, c in sorted(counts.items(), key=lambda kv: -kv[1])]
            behind = [n for n, c in counts.items() if n != brand and c > counts[brand]]
            status = Status.WARNING if behind else Status.PASS
            summary = (f"'{brand}' appears on {counts[brand]} other domain(s) in the top 20 results"
                       + (f"; behind {', '.join(behind)}" if behind else "")
                       + ("" if len(counts) > 1 else " (add competitor URLs to compare)"))
            out.append(CheckResult(spec=self.spec("mentions.share"), status=status, summary=summary, items=items,
                                   affected=len(behind), sources=["Brave Search (mention counts from identical queries)"]))

        out.append(await self._reviews(ctx, brand))
        return out

    async def _reviews(self, ctx: AuditContext, brand: str) -> CheckResult:
        if ctx.llm is None:
            return self.not_checked("mentions.reviews", "Anthropic API key not configured (add the ANTHROPIC_API_KEY secret)")
        try:
            resp = await ctx.search.search(f'"{brand}" reviews', count=20, country=ctx.inputs.country)
        except SearchError as e:
            return self.not_checked("mentions.reviews", str(e))
        snippets = [r for r in resp.results if brand.lower() in (r.title + r.snippet).lower()]
        if len(snippets) < 2:
            return self.not_checked("mentions.reviews", "Not enough publicly accessible review snippets found")
        data_block = "\n".join(f"- [{registered(host_of(r.url))}] {r.title}: {r.snippet}" for r in snippets[:20])
        try:
            data = await ctx.llm.json(task="mentions.reviews", role="bulk", system=REVIEW_SYSTEM,
                                      user=f"Business: {brand}\n<data>\n{data_block}\n</data>",
                                      schema=REVIEW_SCHEMA, max_tokens=1500)
        except LLMError as e:
            return self.not_checked("mentions.reviews", f"LLM analysis failed: {e}")
        status = {"positive": Status.PASS, "mixed": Status.WARNING, "negative": Status.FAIL}.get(data["sentiment"])
        if status is None:
            return self.not_checked("mentions.reviews", data.get("summary") or "Snippets were not reviews of this business")
        return CheckResult(spec=self.spec("mentions.reviews"), status=status,
                           summary=f"Sentiment: {data['sentiment']} — {data['summary']}",
                           items=[FindingItem(None, {"positive_themes": data["positive_themes"][:5],
                                                     "negative_themes": data["negative_themes"][:5],
                                                     "snippets_analysed": len(snippets[:20])})],
                           affected=0 if status == Status.PASS else 1,
                           sources=["Brave Search snippets", f"AI summary ({ctx.settings.llm.bulk_model})"])
