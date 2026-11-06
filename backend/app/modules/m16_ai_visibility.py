"""Module 16: AI Search Visibility — AI-crawler access, llms.txt, and a sampled brand-visibility test.

The visibility test is a SAMPLE: realistic prompts are generated (never containing the brand), each is
run several times (answers vary), and we record whether the brand / competitors are mentioned and
whether the site is cited. Claude (with web search) by default; Gemini when Vertex AI is configured.
"""
from __future__ import annotations

import asyncio
import re

from app.crawler.urls import host_of
from app.datalayer.backlinks import registered
from app.llm.client import BudgetExceeded, LLMError, STR, arr, obj
from app.llm.summaries import select_pages, site_context
from app.modules.base import (
    AuditContext, Category, CheckModule, CheckResult, CheckSpec, FindingItem, Integration, Level, Need, Status,
)
from app.modules.m10_backlinks import brand_of
from app.modules.m12_mentions import competitor_brand

H, M, L = Level.HIGH, Level.MED, Level.LOW
PROMPT_SYSTEM = """You write realistic questions that potential customers would type into an AI assistant
(ChatGPT, Claude, Perplexity, Gemini) when looking for products, services or information in this
business's market — the kind of question where an assistant might recommend or cite businesses like it.
Mix intents: recommendations ("best ... in <place>"), comparisons, how-to questions and buying questions.
Never include the business's own name or domain. Use the target country/language when given."""
PROMPT_SCHEMA = obj({"prompts": arr(STR)})


def mentioned(text: str, names: list[str]) -> bool:
    for n in names:
        n = n.strip()
        if len(n) >= 3 and re.search(r"(?<![\w])" + re.escape(n) + r"(?![\w])", text, re.I):
            return True
    return False


def llms_txt_draft(ctx: AuditContext) -> str:
    site = ctx.site
    home = site.homepage
    name = brand_of(ctx) or site.facts.host
    lines = [f"# {name}", ""]
    if home and home.meta_description:
        lines += [f"> {home.meta_description}", ""]
    lines.append("## Key pages")
    for p in select_pages(site, 15):
        desc = f": {p.meta_description}" if p.meta_description else ""
        lines.append(f"- [{p.title or p.url}]({p.url}){desc}")
    return "\n".join(lines) + "\n"


class AIVisibility(CheckModule):
    key = "ai_visibility"
    number = 16
    name = "AI Search Visibility"
    category = Category.GEO
    needs = frozenset({Need.ROBOTS, Need.SITE_FACTS})
    integrations = frozenset({Integration.LLM})
    depends_on = ("keywords",)
    checks = (
        CheckSpec("ai.crawler_access", "AI crawlers allowed in robots.txt", M, L,
                  "Blocking AI crawlers (GPTBot, ClaudeBot, PerplexityBot…) keeps your content out of their answers "
                  "and citations. Google-Extended only affects Gemini training, not Google Search.",
                  "Allow the AI crawlers you want to be visible in (or keep blocks you chose deliberately)."),
        CheckSpec("ai.llms_txt", "llms.txt file", L, L,
                  "llms.txt is an emerging convention that gives AI tools a curated map of your key content.",
                  "Publish /llms.txt (a draft is generated below).", missing_label="No llms.txt"),
        CheckSpec("ai.visibility", "Brand visibility in AI answers (sample)", H, H,
                  "Whether AI assistants mention or cite you when people ask about your market.",
                  "Strengthen GEO content, earn mentions on sites AI assistants cite, and keep AI crawlers allowed."),
    )

    async def run(self, ctx: AuditContext) -> list[CheckResult]:
        f = ctx.site.facts
        out: list[CheckResult] = []
        bots = f.ai_bots or {}
        if not bots:
            out.append(self.not_checked("ai.crawler_access", "robots.txt rules not available"))
        else:
            blocked = [b for b, r in bots.items() if not r.get("root_allowed")]
            items = [FindingItem(f.robots.get("url"), {"bot": b, "allowed": r.get("root_allowed"),
                                                       "specific_rule": r.get("has_specific_group"),
                                                       "disallow": r.get("disallow")}) for b, r in bots.items()]
            if blocked:
                fix = {"type": "robots_lines", "code": "\n".join(f"User-agent: {b}\nAllow: /\n" for b in blocked),
                       "note": "Only if you want to appear in these assistants; keep deliberate blocks."}
                out.append(CheckResult(spec=self.spec("ai.crawler_access"), status=Status.WARNING,
                                       summary=f"Blocked: {', '.join(blocked)}", items=items, affected=len(blocked),
                                       total=len(bots), generated_fix=fix, sources=["robots.txt"]))
            else:
                out.append(CheckResult(spec=self.spec("ai.crawler_access"), status=Status.PASS,
                                       summary=f"All {len(bots)} AI crawlers checked are allowed", items=items,
                                       total=len(bots), sources=["robots.txt"]))
        llms = f.llms_txt or {}
        if llms.get("present"):
            out.append(self.result("ai.llms_txt", Status.PASS, f"llms.txt present ({llms.get('size', 0)} bytes)",
                                   items=[FindingItem(f.origin + "/llms.txt", {"excerpt": (llms.get("excerpt") or "")[:300]})]))
        else:
            out.append(self.result("ai.llms_txt", Status.WARNING, "No llms.txt at the site root",
                                   items=[FindingItem(f.origin + "/llms.txt", {"status": llms.get("status")})],
                                   generated_fix={"type": "file", "path": "/llms.txt", "content": llms_txt_draft(ctx)},
                                   affected=1))
        out.append(await self._visibility(ctx))
        return out

    async def _visibility(self, ctx: AuditContext) -> CheckResult:
        if ctx.llm is None:
            return self.not_checked("ai.visibility", "Anthropic API key not configured (add the ANTHROPIC_API_KEY secret)")
        brand = brand_of(ctx)
        if not brand:
            return self.not_applicable("ai.visibility", "No brand name provided or detected")
        site_domain = registered(ctx.site.facts.host)
        b = ctx.settings.budgets
        kmap = ctx.shared.get("keyword_map", {})
        topics = sorted({r.get("primary_keyword") for r in kmap.values() if r.get("primary_keyword")})[:15]
        try:
            data = await ctx.llm.json(task="ai_visibility.prompts", role="reasoning",
                                      system=PROMPT_SYSTEM + "\n\nSite context:\n" + site_context(ctx.site, ctx.inputs)
                                      + (f"\nTopics it covers: {', '.join(topics)}" if topics else ""),
                                      user=f"Write {b.ai_visibility_prompts} questions.", schema=PROMPT_SCHEMA,
                                      max_tokens=2000)
        except LLMError as e:
            return self.not_checked("ai.visibility", f"Could not generate prompts: {e}")
        prompts = [p for p in data.get("prompts", []) if not mentioned(p, [brand, site_domain])][:b.ai_visibility_prompts]
        if not prompts:
            return self.not_checked("ai.visibility", "No usable prompts generated")
        comps = {competitor_brand(c): registered(host_of(c)) for c in ctx.inputs.competitors[:3]}
        sem = asyncio.Semaphore(3)
        runs: list[dict] = []
        budget_note = None

        async def one(p: str, k: int) -> None:
            nonlocal budget_note
            async with sem:
                if budget_note:
                    return
                try:
                    a = await ctx.llm.answer(task="ai_visibility.answer", prompt=p, web_search=True, max_uses=2)
                except BudgetExceeded as e:
                    budget_note = str(e)
                    return
                except LLMError as e:
                    runs.append({"prompt": p, "run": k, "error": str(e)})
                    return
                hosts = {registered(host_of(u)) for u in a.cited_urls}
                runs.append({"prompt": p, "run": k, "model": a.model, "searches": a.searched,
                             "brand_mentioned": mentioned(a.text, [brand, site_domain]),
                             "site_cited": any(h == site_domain or h.endswith("." + site_domain) for h in hosts),
                             "competitors": {c: mentioned(a.text, [c, d]) or d in hosts for c, d in comps.items()},
                             "excerpt": a.text[:400]})

        await asyncio.gather(*(one(p, k) for p in prompts for k in range(1, b.ai_visibility_runs + 1)))
        ok = [r for r in runs if "error" not in r]
        if not ok:
            reason = budget_note or (runs[0]["error"] if runs else "No answers")
            return self.not_checked("ai.visibility", reason)
        n = len(ok)
        rate = sum(r["brand_mentioned"] for r in ok) / n
        cite = sum(r["site_cited"] for r in ok) / n
        comp_rates = {c: round(100 * sum(r["competitors"].get(c, False) for r in ok) / n) for c in comps}
        items = []
        for p in prompts:
            rs = [r for r in ok if r["prompt"] == p]
            if not rs:
                continue
            items.append(FindingItem(None, {"prompt": p, "runs": len(rs),
                                            "brand_mentioned": f"{sum(r['brand_mentioned'] for r in rs)}/{len(rs)}",
                                            "site_cited": f"{sum(r['site_cited'] for r in rs)}/{len(rs)}",
                                            **({"competitors_mentioned": {c: f"{sum(r['competitors'].get(c, False) for r in rs)}/{len(rs)}"
                                                                          for c in comps}} if comps else {}),
                                            "sample_answer": rs[0]["excerpt"]}))
        ctx.shared.setdefault("metrics", []).append(("ai_visibility", "runs", ok, "Claude with web search (sample)"))
        model = ok[0]["model"]
        summary = (f"Brand mentioned in {round(100 * rate)}% and site cited in {round(100 * cite)}% of {n} answers "
                   f"({len(prompts)} prompts × up to {b.ai_visibility_runs} runs)"
                   + (f"; competitors: {', '.join(f'{c} {v}%' for c, v in comp_rates.items())}" if comp_rates else "")
                   + (f"; stopped early: {budget_note}" if budget_note else "")
                   + ". A sample, not a guarantee — AI answers vary.")
        sources = [f"Claude ({model}) with web search — sample"]
        sources.append("Gemini (Vertex AI): not configured" if not ctx.integrations.get("vertex_ai_gemini")
                       else "Gemini (Vertex AI): configured, client not built in this phase")
        return CheckResult(spec=self.spec("ai.visibility"), status=Status.PASS if rate >= 0.5 else Status.WARNING,
                           summary=summary, items=items, affected=0 if rate >= 0.5 else 1, sources=sources)
