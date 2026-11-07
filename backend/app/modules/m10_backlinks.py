"""Module 10: Backlink Analysis — authority, discovered backlinks (sample), imported referring domains,
new/lost domains between releases, suspicious patterns and the competitor link gap."""
from __future__ import annotations

import asyncio

from app.crawler.urls import host_of
from app.datalayer import authority, inlinks
from app.datalayer.backlinks import discover, registered, suspicious_patterns
from app.modules.base import (
    AuditContext, Category, CheckModule, CheckResult, CheckSpec, FindingItem, Integration, Level, Need, Status,
)

H, M, L = Level.HIGH, Level.MED, Level.LOW
NO_SEARCH = "Search provider not configured (add the BRAVE_API_KEY secret)"
NO_IMPORT = "Full referring-domain list not available; run the Colab import (notebooks/cc_inlinks.ipynb)"


def brand_of(ctx: AuditContext) -> str | None:
    if ctx.inputs.brand:
        return ctx.inputs.brand
    from app.modules.m09_schema import site_name
    return site_name(ctx.site, ctx)


class BacklinkAnalysis(CheckModule):
    key = "backlinks"
    number = 10
    name = "Backlink Analysis"
    category = Category.OFFPAGE
    needs = frozenset({Need.SITE_FACTS})
    integrations = frozenset({Integration.SEARCH})
    checks = (
        CheckSpec("bl.authority", "Domain Authority Score", M, H,
                  "Authority reflects how well-linked the domain is across the web (Common Crawl web graph).",
                  "Earn links from relevant, authoritative sites (see Link Building & Outreach)."),
        CheckSpec("bl.discovered", "Discovered backlinks (sample)", M, H,
                  "A sample of pages that link to the site, found via search and verified by fetching them.",
                  "Build more links from relevant sites; reclaim unlinked mentions."),
        CheckSpec("bl.referring_domains", "Referring domains (full list) and new/lost domains", M, H,
                  "The complete set of linking domains and how it changes between Common Crawl releases.",
                  "Investigate lost referring domains; keep earning new ones."),
        CheckSpec("bl.suspicious", "Suspicious backlink patterns", M, M,
                  "Link-farm-like or spammy referrers can indicate low-quality link building.",
                  "Review the flagged referrers; remove paid/spam links where you can."),
        CheckSpec("bl.link_gap", "Competitor link gap", M, H,
                  "Domains that link to competitors but not to you are the most likely to link to you too.",
                  "Pitch these domains (they are added to the outreach list)."),
    )

    async def run(self, ctx: AuditContext) -> list[CheckResult]:
        site_domain = registered(ctx.site.facts.host)
        brand = brand_of(ctx)
        out: list[CheckResult] = []

        # authority
        auth = (await asyncio.to_thread(authority.lookup, [site_domain]))[site_domain]
        if auth is None:
            out.append(self.not_checked("bl.authority", "Authority table not loaded yet (Common Crawl job pending)"))
        else:
            src = [authority.source_label(auth.get("release"))]
            if auth.get("score") is None:
                out.append(self.result("bl.authority", Status.PASS, auth["label"], sources=src,
                                       items=[FindingItem(None, {"domain": site_domain, "authority": auth["label"]})]))
            else:
                prev, cur = auth.get("previous_score"), auth["score"]
                drop = prev is not None and cur <= prev - 5
                summary = f"Authority Score {cur}/100" + (f" (previous release: {prev})" if prev is not None else "")
                out.append(self.result("bl.authority", Status.WARNING if drop else Status.PASS, summary, sources=src,
                                       items=[FindingItem(None, {"domain": site_domain, "score": cur, "previous": prev,
                                                                 "harmonic_rank": auth.get("harmonic_rank")})],
                                       affected=1 if drop else 0))

        # discovered backlinks (sample)
        disc = None
        if ctx.search is None:
            out.append(self.not_checked("bl.discovered", NO_SEARCH))
        else:
            disc = await discover(site_domain, brand, ctx.search, ctx.settings,
                                  max_pages=ctx.settings.data.backlink_pages_checked, country=ctx.inputs.country)
            ctx.shared["discovery"] = disc
            ctx.shared.setdefault("metrics", []).append(("backlinks", "discovery", disc.to_json(), "Brave Search + fetched pages"))
            if not disc.queries:
                out.append(self.not_checked("bl.discovered", f"Search failed: {disc.errors[0] if disc.errors else 'unknown'}"))
            elif disc.candidates and not disc.pages_checked:
                out.append(self.not_checked("bl.discovered", f"None of the {disc.candidates} candidate pages could be "
                                                             f"fetched to verify links (first error: {disc.errors[0] if disc.errors else 'unknown'})"))
            else:
                items = [FindingItem(b.source_url, {"links_to": b.target_url, "anchor": b.anchor or "(no text)",
                                                    "rel": ",".join(k for k in ("nofollow", "sponsored", "ugc") if getattr(b, k)) or "follow",
                                                    "source_authority": b.authority if b.authority is not None else "low/unknown"})
                         for b in sorted(disc.backlinks, key=lambda b: -(b.authority or 0))]
                summary = (f"Discovered backlinks (sample): {len(disc.backlinks)} link(s) from {len(disc.referring_domains)} "
                           f"domain(s), verified on {disc.pages_checked} of {disc.candidates} candidate page(s)")
                status = Status.PASS if disc.backlinks else Status.WARNING
                out.append(CheckResult(spec=self.spec("bl.discovered"), status=status, summary=summary, items=items,
                                       affected=0 if disc.backlinks else 1,
                                       sources=["Brave Search", "Pages fetched and checked for links",
                                                "Common Crawl (source authority)"]))

        # full referring-domain list (Colab import)
        full = await asyncio.to_thread(inlinks.referring_domains, site_domain)
        new_prev = None
        if full is None:
            out.append(self.not_checked("bl.referring_domains", NO_IMPORT + (
                f"; showing the discovered sample instead ({len(disc.referring_domains)} domain(s))" if disc else "")))
        else:
            doms = full["domains"]
            ev = {"referring_domains": len(doms), "release": full["release"]}
            items = []
            if full["previous_domains"] is not None:
                new = doms - full["previous_domains"]
                lost = full["previous_domains"] - doms
                new_prev = (len(new), len(full["previous_domains"]))
                ev.update({"previous_release": full["previous_release"], "new": len(new), "lost": len(lost)})
                items = [FindingItem(None, {"lost_domain": d}) for d in sorted(lost)[:200]]
            status = Status.WARNING if items else Status.PASS
            out.append(CheckResult(spec=self.spec("bl.referring_domains"), status=status,
                                   summary=f"{len(doms)} referring domains ({full['release']})"
                                           + (f"; {ev['new']} new, {ev['lost']} lost since {ev['previous_release']}"
                                              if "new" in ev else ""),
                                   items=[FindingItem(None, ev)] + items, affected=len(items),
                                   sources=[authority.source_label(full["release"]) + " (imported inlinks)"]))

        # suspicious patterns
        if full is not None:
            ref_auth = await asyncio.to_thread(authority.lookup, sorted(full["domains"])[:20000])
            dom_scores = {d: (ref_auth.get(d) or {}).get("score") for d in full["domains"]}
            basis = "full imported list"
        elif disc is not None:
            dom_scores = {b.source_domain: b.authority for b in disc.backlinks}
            basis = "discovered sample"
        else:
            dom_scores = None
        if dom_scores is None:
            out.append(self.not_checked("bl.suspicious", NO_SEARCH))
        elif not dom_scores:
            out.append(self.not_applicable("bl.suspicious", "No referring domains to analyse"))
        else:
            outbound = {b.source_domain: b.outbound_domains for b in (disc.backlinks if disc else [])}
            flags = suspicious_patterns(dom_scores, outbound=outbound, new_vs_previous=new_prev)
            items = [FindingItem(None, f) for f in flags]
            out.append(CheckResult(spec=self.spec("bl.suspicious"), status=Status.WARNING if flags else Status.PASS,
                                   summary=(f"{len(flags)} suspicious pattern(s) in the {basis}" if flags
                                            else f"No suspicious patterns in the {basis} ({len(dom_scores)} domains)"),
                                   items=items, affected=len(flags), total=len(dom_scores),
                                   sources=["Heuristics on " + basis]))

        out.append(await self._link_gap(ctx, site_domain, full, disc))
        return out

    async def _link_gap(self, ctx: AuditContext, site_domain: str, full, disc) -> CheckResult:
        comps = [registered(host_of(c)) for c in ctx.inputs.competitors[:3]]
        if not comps:
            return self.not_applicable("bl.link_gap", "No competitor URLs were provided with the audit")
        ours = (full or {}).get("domains") or (disc.referring_domains if disc else set())
        gap: dict[str, set[str]] = {}
        basis = []
        for c in comps:
            cfull = await asyncio.to_thread(inlinks.referring_domains, c)
            if cfull:
                theirs, b = cfull["domains"], "imported"
            elif ctx.search is not None:
                cd = await discover(c, None, ctx.search, ctx.settings, max_pages=10, country=ctx.inputs.country)
                theirs, b = cd.referring_domains, "sample"
            else:
                continue
            basis.append(f"{c}: {b}")
            for d in theirs - ours - {site_domain}:
                gap.setdefault(d, set()).add(c)
        if not basis:
            return self.not_checked("bl.link_gap", NO_SEARCH)
        auth = await asyncio.to_thread(authority.lookup, list(gap)) if gap else {}
        ranked = sorted(gap.items(), key=lambda kv: (-len(kv[1]), -((auth.get(kv[0]) or {}).get("score") or 0)))
        ctx.shared["link_gap"] = [{"domain": d, "links_to": sorted(cs), "authority": (auth.get(d) or {}).get("score")}
                                  for d, cs in ranked[:50]]
        items = [FindingItem(None, g) for g in ctx.shared["link_gap"]]
        status = Status.WARNING if items else Status.PASS
        return CheckResult(spec=self.spec("bl.link_gap"), status=status,
                           summary=f"{len(gap)} domain(s) link to competitors but not to you ({'; '.join(basis)})",
                           items=items, affected=len(gap),
                           sources=["Common Crawl inlinks (imported)" if any("imported" in b for b in basis)
                                    else "Discovered backlink samples (Brave Search)"])
