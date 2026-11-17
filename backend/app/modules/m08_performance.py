"""Module 8: Website Performance (PageSpeed Insights, mobile + desktop).

Tests the homepage plus one representative URL per page template (budget-capped).
Real-user Core Web Vitals (CrUX field data) and Lighthouse lab results are separate checks, so the two
kinds of data are never mixed; each value is labelled with its source.
"""
from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import timedelta
from urllib.parse import urlsplit

from app import cache, usage
from app.config import env
from app.modules.base import (
    AuditContext, Category, CheckModule, CheckResult, CheckSpec, FindingItem, Integration, Level, Need, Status,
)
from app.providers.pagespeed import (
    SOURCE_FIELD, SOURCE_LAB, SOURCE_ORIGIN, PageSpeedClient, PsiResult, rate,
)
from app.sitemodel.types import PageData

H, M, L = Level.HIGH, Level.MED, Level.LOW
STRATEGIES = ("mobile", "desktop")
CACHE_TTL = timedelta(hours=24)
NO_KEY = "PageSpeed API key not configured (add the PAGESPEED_API_KEY secret)"

FIX_HINTS = {
    "render-blocking-insight": "Inline critical CSS, load the rest async, and add defer/async to scripts in <head>.",
    "image-delivery-insight": "Serve images in WebP/AVIF at the displayed size with srcset; compress them.",
    "cache-insight": "Set long Cache-Control max-age (e.g. 1 year + file hashing) for static assets.",
    "unused-css-rules": "Remove unused CSS or split it per page; purge framework CSS.",
    "unused-javascript": "Remove unused JavaScript, code-split, and lazy-load non-critical bundles.",
    "unminified-css": "Minify CSS in the build.",
    "unminified-javascript": "Minify JavaScript in the build.",
    "document-latency-insight": "Reduce server response time, enable text compression (gzip/brotli), avoid redirects.",
    "font-display-insight": "Use font-display: swap and preload the main web fonts.",
    "legacy-javascript-insight": "Stop shipping polyfills/transpiled code to modern browsers.",
    "duplicated-javascript-insight": "Deduplicate JavaScript modules shared across bundles.",
    "third-parties-insight": "Defer or remove heavy third-party scripts (chat widgets, trackers, embeds).",
    "lcp-discovery-insight": "Make the LCP image discoverable in HTML; add fetchpriority=\"high\", no lazy-loading.",
    "network-dependency-tree-insight": "Shorten critical request chains; preconnect to required origins.",
    "dom-size-insight": "Reduce DOM size (fewer nested wrappers, paginate long lists).",
    "forced-reflow-insight": "Avoid reading layout properties right after DOM writes in JavaScript.",
}


def pick_pages(ctx: AuditContext, n_templates: int) -> list[PageData]:
    """Homepage + the most-linked indexable page of each of the largest templates."""
    site = ctx.site
    home = site.homepage
    chosen = [home] if home and home.is_ok else []
    groups: dict[str, list[PageData]] = defaultdict(list)
    for p in site.indexable_pages():
        if home is None or p.url != home.url:
            groups[p.template_key or "/"].append(p)
    for _, pages in sorted(groups.items(), key=lambda kv: -len(kv[1]))[:n_templates]:
        chosen.append(max(pages, key=lambda p: (p.inlinks, -(p.depth or 99))))
    return chosen


async def _fetch_one(client: PageSpeedClient, url: str, strategy: str, audit_id: int | None) -> PsiResult:
    key = cache.cache_key("psi-v5", url, strategy)
    hit = await asyncio.to_thread(cache.get, "pagespeed", key)
    if hit:
        r = PsiResult.from_json(hit)
        return r
    r = await client.run(url, strategy)
    await asyncio.to_thread(usage.record, "pagespeed", "runPagespeed", 1, 0.0, audit_id)
    if not r.error:
        await asyncio.to_thread(cache.put, "pagespeed", key, r.to_json(), CACHE_TTL)
    return r


class Performance(CheckModule):
    key = "performance"
    number = 8
    name = "Website Performance"
    category = Category.PERFORMANCE
    needs = frozenset({Need.PAGES})
    integrations = frozenset({Integration.PAGESPEED})
    checks = (
        CheckSpec("perf.field_cwv", "Core Web Vitals from real visitors (Chrome UX Report)", H, M,
                  "Google ranks with Core Web Vitals measured on real Chrome users over the last 28 days "
                  "(75th percentile): LCP ≤ 2.5 s, INP ≤ 200 ms, CLS ≤ 0.1.",
                  "Fix the metric that is outside 'good' (see the lab checks and opportunities for causes); "
                  "field data updates over the following 28 days."),
        CheckSpec("perf.lcp", "Largest Contentful Paint – lab test (≤ 2.5 s)", M, M,
                  "Lab LCP (Lighthouse, simulated device) shows how fast the main content appears and why.",
                  "Optimise the LCP element: compress/resize the hero image, preload it, cut render-blocking "
                  "CSS/JS and reduce server response time.", population="tests"),
        CheckSpec("perf.cls", "Cumulative Layout Shift – lab test (≤ 0.1)", M, L,
                  "Lab CLS shows unexpected layout movement during load.",
                  "Set width/height on images and embeds, reserve space for ads/banners, avoid inserting "
                  "content above existing content.", population="tests"),
        CheckSpec("perf.tbt", "Total Blocking Time – lab test (proxy for INP, ≤ 200 ms)", M, M,
                  "High blocking time in the lab usually means slow responses to user input.",
                  "Reduce and split JavaScript; defer non-critical scripts.", population="tests"),
        CheckSpec("perf.score", "Lighthouse performance score (≥ 90)", M, M,
                  "A summary of loading performance in Google's lab test.",
                  "Work through the opportunities listed in the performance findings.", population="tests"),
        CheckSpec("perf.opportunities", "Top performance opportunities", M, M,
                  "Specific changes with the largest estimated time or byte savings.",
                  "Apply the listed fixes, starting with the largest savings."),
    )

    async def run(self, ctx: AuditContext) -> list[CheckResult]:
        if not ctx.integrations.get("pagespeed"):
            return [self.not_checked(c.id, NO_KEY) for c in self.checks]
        pages = pick_pages(ctx, ctx.settings.budgets.pagespeed_templates)
        if not pages:
            return [self.not_applicable(c.id, "No indexable pages to test") for c in self.checks]
        client = PageSpeedClient(env().pagespeed_api_key)
        sem = asyncio.Semaphore(4)

        async def one(url: str, strategy: str) -> PsiResult:
            async with sem:
                return await _fetch_one(client, url, strategy, ctx.audit_id)

        results = await asyncio.gather(*(one(p.url, s) for p in pages for s in STRATEGIES))
        ok = [r for r in results if not r.error]
        for r in results:
            ctx.shared.setdefault("metrics", []).append(
                ("performance", f"psi:{r.strategy}:{r.url}", r.to_json(),
                 SOURCE_FIELD if r.field else SOURCE_LAB))
        if not ok:
            reason = f"PageSpeed Insights failed for every URL: {results[0].error}"
            return [self.not_checked(c.id, reason) for c in self.checks]
        failed = [FindingItem(r.url, {"strategy": r.strategy, "error": r.error}) for r in results if r.error]

        out = [self._field_cwv(ok),
               self._cwv("perf.lcp", ok, "lcp_ms"),
               self._cwv("perf.cls", ok, "cls"),
               self._lab("perf.tbt", ok, "tbt_ms"),
               self._score(ok),
               self._opportunities(ok)]
        if failed:
            for r in out:
                if r.status != Status.NOT_CHECKED:
                    r.summary = (r.summary + f" · {len(failed)} test(s) failed: "
                                 + "; ".join(f"{i.url} ({i.evidence['strategy']}): {i.evidence['error']}"
                                             for i in failed[:3]))
        return out

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _fmt(metric: str, v: float) -> str:
        if metric == "cls":
            return f"{v:.2f}"
        return f"{v / 1000:.1f} s" if metric == "lcp_ms" else f"{round(v)} ms"

    def _field_cwv(self, results: list[PsiResult]) -> CheckResult:
        """Real-user CrUX data only: URL-level when Google has it, otherwise the whole origin (labelled)."""
        entries: dict[tuple[str, str], dict] = {}
        for r in results:
            if r.field:
                key, data, scope, src = (r.url, r.strategy), r.field, "this page", SOURCE_FIELD
            elif r.origin_field:
                origin = "{0.scheme}://{0.netloc}/".format(urlsplit(r.final_url or r.url))
                key, data, scope, src = (origin, r.strategy), r.origin_field, "whole site (origin)", SOURCE_ORIGIN
            else:
                continue
            if key in entries:
                continue
            ev: dict = {"device": r.strategy, "scope": scope, "source": src}
            worst = "good"
            for metric, label in (("lcp_ms", "LCP"), ("inp_ms", "INP"), ("cls", "CLS")):
                if metric in data:
                    rating = rate(metric, data[metric])
                    ev[label] = f"{self._fmt(metric, data[metric])} ({rating})"
                    if rating == "poor" or (rating != "good" and worst == "good"):
                        worst = rating
            ev["assessment"] = worst
            entries[key] = {"url": key[0], "ev": ev, "src": src}
        if not entries:
            return self.not_checked("perf.field_cwv", "Google has no real-visitor (Chrome UX Report) data for these "
                                                      "pages or the site yet; it needs enough Chrome traffic. The lab "
                                                      "test results are the only speed measurement.")
        bad = [e for e in entries.values() if e["ev"]["assessment"] != "good"]
        status = (Status.PASS if not bad else Status.FAIL if any(e["ev"]["assessment"] == "poor" for e in bad)
                  else Status.WARNING)
        items = [FindingItem(e["url"], e["ev"]) for e in (bad or entries.values())]
        summary = (f"All {len(entries)} real-visitor measurement(s) are 'good'" if not bad
                   else f"{len(bad)} of {len(entries)} real-visitor measurement(s) outside 'good'")
        return CheckResult(spec=self.spec("perf.field_cwv"), status=status, summary=summary, items=items,
                           affected=len(bad), total=len(entries), sources=sorted({e["src"] for e in entries.values()}))

    def _cwv(self, cid: str, results: list[PsiResult], metric: str) -> CheckResult:
        """Lab (Lighthouse) value of a Core Web Vital; real-user data is reported by perf.field_cwv."""
        items, all_items, measured, worst = [], [], 0, "good"
        for r in results:
            if metric not in r.lab:
                continue
            v = r.lab[metric]
            measured += 1
            rating = rate(metric, v)
            item = FindingItem(r.url, {"device": r.strategy, "value": self._fmt(metric, v), "rating": rating,
                                       "source": SOURCE_LAB})
            all_items.append(item)
            if rating != "good":
                worst = "poor" if "poor" in (rating, worst) else "needs improvement"
                items.append(item)
        if measured == 0:
            return self.not_checked(cid, "No lab measurement returned")
        status = Status.PASS if not items else Status.FAIL if worst == "poor" else Status.WARNING
        summary = (f"All {measured} lab test(s) within the 'good' threshold" if not items
                   else f"{len(items)} of {measured} lab test(s) outside 'good'")
        # passing checks keep every measurement as evidence; failing ones list only the failures
        return CheckResult(spec=self.spec(cid), status=status, summary=summary, items=items or all_items,
                           affected=len(items), total=measured, sources=[SOURCE_LAB])

    def _lab(self, cid: str, results: list[PsiResult], metric: str) -> CheckResult:
        items, worst = [], "good"
        for r in results:
            v = r.lab.get(metric)
            if v is None:
                continue
            rating = rate(metric, v)
            if rating != "good":
                worst = "poor" if "poor" in (rating, worst) else "needs improvement"
                items.append(FindingItem(r.url, {"strategy": r.strategy, "value": self._fmt(metric, v),
                                                 "rating": rating}))
        status = Status.PASS if not items else Status.FAIL if worst == "poor" else Status.WARNING
        return CheckResult(spec=self.spec(cid), status=status,
                           summary="Within 200 ms everywhere" if not items else f"{len(items)} test(s) above 200 ms",
                           items=items, affected=len(items), total=len(results), sources=[SOURCE_LAB])

    def _score(self, results: list[PsiResult]) -> CheckResult:
        scored = [r for r in results if r.score is not None]
        items = [FindingItem(r.url, {"strategy": r.strategy, "score": r.score}) for r in scored if r.score < 90]
        status = Status.PASS if not items else Status.FAIL if any(r.score < 50 for r in scored) else Status.WARNING
        avg = {s: round(sum(r.score for r in scored if r.strategy == s) / max(1, sum(1 for r in scored if r.strategy == s)))
               for s in STRATEGIES if any(r.strategy == s for r in scored)}
        return CheckResult(spec=self.spec("perf.score"), status=status,
                           summary="Average score " + ", ".join(f"{k} {v}" for k, v in avg.items()),
                           items=sorted(items, key=lambda i: i.evidence["score"]), affected=len(items),
                           total=len(scored), sources=[SOURCE_LAB])

    def _opportunities(self, results: list[PsiResult]) -> CheckResult:
        agg: dict[str, dict] = {}
        for r in results:
            for o in r.opportunities:
                a = agg.setdefault(o["id"], {"title": o["title"], "max_savings_ms": 0, "max_savings_kb": 0,
                                             "pages": [], "strategies": set()})
                a["max_savings_ms"] = max(a["max_savings_ms"], o["savings_ms"])
                a["max_savings_kb"] = max(a["max_savings_kb"], o["savings_kb"])
                if r.url not in a["pages"]:
                    a["pages"].append(r.url)
                a["strategies"].add(r.strategy)
        significant = {k: v for k, v in agg.items() if v["max_savings_ms"] >= 100 or v["max_savings_kb"] >= 20}
        if not significant:
            return self.result("perf.opportunities", Status.PASS, "No significant savings reported",
                               sources=[SOURCE_LAB])
        items = []
        for k, v in sorted(significant.items(), key=lambda kv: (-kv[1]["max_savings_ms"], -kv[1]["max_savings_kb"])):
            ev = {"opportunity": v["title"], "est_savings_ms": v["max_savings_ms"] or None,
                  "est_savings_kb": v["max_savings_kb"] or None, "strategies": sorted(v["strategies"]),
                  "pages": len(v["pages"])}
            fix = {"type": "advice", "note": FIX_HINTS.get(k, "See the PageSpeed Insights report for details.")}
            items.append(FindingItem(v["pages"][0], ev, generated_fix=fix, also_on=v["pages"][1:]))
        return CheckResult(spec=self.spec("perf.opportunities"), status=Status.WARNING,
                           summary=f"{len(items)} improvement(s) with estimated savings; largest: {items[0].evidence['opportunity']}",
                           items=items, affected=len(items), sources=[SOURCE_LAB])
