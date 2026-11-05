"""Module 7: Crawling & Indexing (crawl-based; Search Console checks when connected)."""
from __future__ import annotations

from collections import Counter
from xml.sax.saxutils import escape

from app.modules.base import (
    AuditContext, Category, CheckModule, CheckResult, CheckSpec, FindingItem, Integration, Level, Need,
    Status,
)
from app.modules.common import cached_link_sources, internal_targets, is_utility

H, M, L = Level.HIGH, Level.MED, Level.LOW
GSC_SETUP = ("Requires Google Search Console access. Setup: create a Google Cloud service account, "
             "add its e-mail as a user on the Search Console property, and save the JSON key as the "
             "GSC_SERVICE_ACCOUNT_JSON secret.")
MAX_SITEMAP_FIX_URLS = 1000


def _sitemap_xml(urls: list[str]) -> str:
    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    lines += [f"  <url><loc>{escape(u)}</loc></url>" for u in urls[:MAX_SITEMAP_FIX_URLS]]
    lines.append("</urlset>")
    return "\n".join(lines)


class CrawlingIndexing(CheckModule):
    key = "indexing"
    number = 7
    name = "Crawling & Indexing"
    category = Category.TECHNICAL
    needs = frozenset({Need.PAGES, Need.ROBOTS, Need.SITEMAP})
    integrations = frozenset({Integration.SEARCH_CONSOLE})
    checks = (
        CheckSpec("crawl.robots_txt", "robots.txt available", M, L,
                  "robots.txt tells crawlers what they may fetch; a failing robots.txt (5xx) makes Google stop crawling.",
                  "Serve a valid robots.txt at the site root that returns 200 (or 404 if you want no rules).",
                  missing_label="No robots.txt"),
        CheckSpec("crawl.robots_blocks_important", "robots.txt blocks important URLs", H, L,
                  "Blocked URLs cannot be crawled, so their content cannot rank.",
                  "Remove or narrow the Disallow rules that match these URLs.", population="pages"),
        CheckSpec("crawl.robots_blocks_resources", "robots.txt blocks CSS/JS needed for rendering", M, L,
                  "Google renders pages; blocked CSS/JS can make pages look broken or empty to it.",
                  "Allow crawling of CSS and JavaScript files.", population="pages"),
        CheckSpec("crawl.robots_sitemap_directive", "robots.txt references the sitemap", L, L,
                  "A Sitemap: line helps every crawler find your sitemap.",
                  "Add 'Sitemap: https://example.com/sitemap.xml' to robots.txt."),
        CheckSpec("crawl.sitemap_present", "XML sitemap present", M, L,
                  "Sitemaps help search engines discover and prioritise your pages.",
                  "Publish an XML sitemap of indexable URLs and reference it in robots.txt.",
                  missing_label="No XML sitemap"),
        CheckSpec("crawl.sitemap_errors", "Sitemap files load and parse", M, L,
                  "Unreadable sitemaps are ignored.",
                  "Fix the sitemap URL/HTTP status and make sure it is valid sitemap XML."),
        CheckSpec("crawl.sitemap_bad_urls", "Sitemap lists non-200 URLs", M, L,
                  "Listing errors and redirects in the sitemap wastes crawl budget and reduces trust in it.",
                  "List only final, 200-status URLs.", population="sitemap_urls"),
        CheckSpec("crawl.sitemap_non_indexable", "Sitemap lists noindexed, canonicalised or blocked URLs", M, L,
                  "Sitemaps should contain only URLs you want indexed; mixed signals confuse search engines.",
                  "Remove these URLs from the sitemap, or make them indexable.", population="sitemap_urls"),
        CheckSpec("crawl.sitemap_missing_pages", "Indexable pages missing from the sitemap", M, L,
                  "Pages missing from the sitemap are discovered more slowly.",
                  "Add every indexable, canonical page to the sitemap.", population="pages"),
        CheckSpec("crawl.noindex_linked", "Noindexed pages that receive many internal links", L, M,
                  "Linking heavily to noindexed pages spends crawl budget and link equity on pages that cannot rank.",
                  "Either make these pages indexable or reduce internal links to them.", population="pages"),
        CheckSpec("crawl.gsc_index_coverage", "Indexed vs not-indexed pages (Search Console)", H, L,
                  "Shows which pages Google actually indexed and why others were excluded.",
                  "Review 'Crawled – currently not indexed' and 'Discovered – currently not indexed' URLs."),
        CheckSpec("crawl.gsc_url_inspection", "URL Inspection for key pages (Search Console)", M, L,
                  "Confirms Google's view of key pages (indexed, canonical chosen, last crawl).",
                  "Fix issues reported for key pages."),
    )

    async def run(self, ctx: AuditContext) -> list[CheckResult]:
        site = ctx.site
        f = site.facts
        robots = site.robots
        sources = cached_link_sources(ctx)
        internal = internal_targets(ctx)
        ok = site.html_pages()
        out: list[CheckResult] = []

        # robots.txt availability
        st = f.robots.get("status")
        if st == 200:
            out.append(self.result("crawl.robots_txt", Status.PASS, "robots.txt returns 200",
                                   items=[FindingItem(f.robots.get("url"), {"status": st, "size": f.robots.get("size")})]))
        elif st is not None and 400 <= st < 500:
            fix = {"type": "file", "path": "/robots.txt",
                   "content": "User-agent: *\nDisallow:\n\n" + (f"Sitemap: {f.origin}/sitemap.xml\n")}
            note = f.robots.get("note")
            out.append(self.result("crawl.robots_txt", Status.WARNING,
                                   note or f"robots.txt returns {st} (no rules)",
                                   items=[FindingItem(f.robots.get("url"),
                                                      {"status": f.robots.get("http_status", st), "note": note})],
                                   generated_fix=fix, impact=L, affected=1))
        else:
            out.append(self.result("crawl.robots_txt", Status.FAIL,
                                   "robots.txt is unreachable (5xx/timeout): Google pauses crawling",
                                   items=[FindingItem(f.robots.get("url"), {"status": st, "error": f.robots.get("error")})],
                                   impact=H, affected=1))

        # robots blocking important URLs (evaluated as Googlebot)
        if robots is None or not robots.fetched_ok:
            out.append(self.not_applicable("crawl.robots_blocks_important", "No robots.txt rules to evaluate"))
            out.append(self.not_applicable("crawl.robots_blocks_resources", "No robots.txt rules to evaluate"))
        else:
            candidates = set(internal) | set(site.sitemap_urls) | {f.start_url}
            blocked = []
            for u in sorted(candidates):
                if robots.can_fetch(u, "Googlebot") or is_utility(u):
                    continue
                n_links = len(sources.get(u, []))
                important = u == f.start_url or u in site.sitemap_urls or n_links >= ctx.settings.thresholds.important_page_min_inlinks
                if important:
                    group = robots.group_for("Googlebot")
                    blocked.append(FindingItem(u, {
                        "in_sitemap": u in site.sitemap_urls, "linked_from_pages": n_links,
                        "matching_group": group.agents if group else None,
                        "disallow_rules": [p for d, p in (group.rules if group else []) if d == "disallow"][:10]}))
            r = self.by_population("crawl.robots_blocks_important", blocked, len(candidates),
                                   summary="robots.txt blocks URLs that are linked, listed in the sitemap, or the homepage")
            r.sources = ["robots.txt evaluated as Googlebot"]
            out.append(r)
            res_items = []
            for p in ok:
                bad = [x for x in p.resources if not robots.can_fetch(x, "Googlebot")]
                if bad:
                    res_items.append(FindingItem(p.url, {"blocked_resources": bad[:10], "count": len(bad)}))
            r = self.by_population("crawl.robots_blocks_resources", res_items, len(ok), fail=False,
                                   summary="CSS/JS files are disallowed for Googlebot")
            r.sources = ["robots.txt evaluated as Googlebot"]
            out.append(r)

        # Sitemap directive
        if st == 200:
            if f.robots.get("sitemaps"):
                out.append(self.result("crawl.robots_sitemap_directive", Status.PASS,
                                       f"{len(f.robots['sitemaps'])} Sitemap: line(s) found",
                                       items=[FindingItem(None, {"sitemaps": f.robots["sitemaps"]})]))
            else:
                found = [s["url"] for s in f.sitemaps if s.get("status") == 200]
                out.append(self.result("crawl.robots_sitemap_directive", Status.WARNING,
                                       "robots.txt has no Sitemap: line",
                                       generated_fix={"type": "robots_line",
                                                      "code": f"Sitemap: {found[0] if found else f.origin + '/sitemap.xml'}"},
                                       affected=1))
        else:
            out.append(self.not_applicable("crawl.robots_sitemap_directive", "No robots.txt file"))

        # Sitemap presence / errors
        indexable_urls = sorted(p.url for p in site.indexable_pages())
        if f.sitemap_found:
            out.append(self.result("crawl.sitemap_present", Status.PASS,
                                   f"{f.sitemap_url_count} URL(s) in {len(f.sitemaps)} sitemap file(s)",
                                   items=[FindingItem(s["url"], {k: s[k] for k in ("status", "kind", "url_count", "source")})
                                          for s in f.sitemaps]))
        else:
            out.append(self.result("crawl.sitemap_present", Status.FAIL, "No XML sitemap found",
                                   items=[FindingItem(None, {
                                       "checked": [s["url"] for s in f.sitemaps] or
                                       ["robots.txt Sitemap: lines", "/sitemap.xml", "/sitemap_index.xml",
                                        "/sitemap-index.xml", "/wp-sitemap.xml"],
                                       "returned_html_not_xml": f.sitemap_soft_404_probes})],
                                   generated_fix={"type": "file", "path": "/sitemap.xml",
                                                  "content": _sitemap_xml(indexable_urls),
                                                  "note": "Built from the indexable pages found in this crawl"},
                                   affected=1))
        errs = [FindingItem(s["url"], {"status": s.get("status"), "error": s.get("error")})
                for s in f.sitemaps if s.get("error")]
        if not f.sitemaps:
            out.append(self.not_applicable("crawl.sitemap_errors", "No sitemap files found"))
        else:
            out.append(self.by_population("crawl.sitemap_errors", errs, len(f.sitemaps),
                                          summary="Sitemap files failed to load or parse"))

        # Sitemap URL quality
        sm = site.sitemap_urls
        if not sm:
            for cid in ("crawl.sitemap_bad_urls", "crawl.sitemap_non_indexable", "crawl.sitemap_missing_pages"):
                out.append(self.not_applicable(cid, "No sitemap URLs to evaluate"))
        else:
            bad, nonidx = [], []
            known = 0
            off_host = 0
            for u in sm:
                s = site.status_of(u)
                if s is None:
                    if not u.startswith(f.origin) and not any(u.startswith(o) for o in
                                                              (f.origin.replace("://", "://www."),
                                                               f.origin.replace("://www.", "://"))):
                        off_host += 1
                        bad.append(FindingItem(u, {"problem": "URL is on a different host than the site"}))
                    continue
                known += 1
                if s.blocked_by_robots:
                    nonidx.append(FindingItem(u, {"problem": "Blocked by robots.txt"}))
                    continue
                if s.redirects:
                    bad.append(FindingItem(u, {"status": s.status, "redirects_to": s.final_url, "severity": "warning"}))
                elif s.status != 200:
                    bad.append(FindingItem(u, {"status": s.status, "error": s.error}))
                p = site.pages.get(u)
                if p is not None and p.is_ok:
                    if p.is_noindex:
                        nonidx.append(FindingItem(u, {"problem": "noindex", "meta_robots": p.meta_robots,
                                                      "x_robots_tag": p.x_robots_tag}))
                    elif not p.canonical_is_self:
                        nonidx.append(FindingItem(u, {"problem": "Canonicalised to another URL",
                                                      "canonical": p.canonical_target}))
            hard = [i for i in bad if i.evidence.get("severity") != "warning"]
            r = self.by_population("crawl.sitemap_bad_urls", bad, known + off_host, fail=bool(hard),
                                   summary=f"{len(bad)} sitemap URL(s) do not return 200 directly")
            if known < len(sm):
                r.summary += f" ({known} of {len(sm)} sitemap URLs were checked)"
            out.append(r)
            out.append(self.by_population("crawl.sitemap_non_indexable", nonidx, known, fail=False,
                                          summary="Sitemap lists URLs that cannot be indexed"))
            if f.sitemap_truncated:
                out.append(self.not_checked("crawl.sitemap_missing_pages",
                                            "Sitemap is larger than the configured URL limit; comparison would be incomplete"))
            else:
                missing = [FindingItem(u, {"depth": site.pages[u].depth, "inlinks": site.pages[u].inlinks})
                           for u in indexable_urls if u not in sm]
                out.append(self.by_population("crawl.sitemap_missing_pages", missing, len(indexable_urls), fail=False,
                                              summary="Indexable pages are not listed in the sitemap",
                                              generated_fix={"type": "sitemap_entries",
                                                             "content": "\n".join(f"<url><loc>{escape(i.url)}</loc></url>"
                                                                                  for i in missing[:MAX_SITEMAP_FIX_URLS])}
                                              if missing else None))

        # noindexed pages that are heavily linked
        html_n = max(1, len(ok))
        thr = max(ctx.settings.thresholds.important_page_min_inlinks, int(0.2 * html_n))
        nl = [FindingItem(p.url, {"inlinks": p.inlinks, "meta_robots": p.meta_robots})
              for p in ok if p.is_noindex and p.inlinks >= thr and not is_utility(p.url)]
        out.append(self.by_population("crawl.noindex_linked", nl, len(ok), fail=False,
                                      summary=f"Noindexed pages linked from {thr}+ pages"))

        # indexability summary metric
        reasons: Counter[str] = Counter()
        for p in site.pages.values():
            if p.indexable:
                reasons["indexable"] += 1
            elif p.blocked_by_robots:
                reasons["blocked_by_robots"] += 1
            elif p.status is None:
                reasons["fetch_error"] += 1
            elif p.redirect_chain:
                reasons["redirect"] += 1
            elif p.status != 200:
                reasons[f"http_{p.status}"] += 1
            elif not p.is_html:
                reasons["not_html"] += 1
            elif p.is_noindex:
                reasons["noindex"] += 1
            elif not p.canonical_is_self:
                reasons["canonicalised"] += 1
        ctx.shared.setdefault("metrics", []).append(
            ("indexing", "indexability_breakdown", dict(reasons), "Crawl"))

        if ctx.integrations.get("search_console"):
            reason = "Search Console credentials are set, but the integration is built in a later phase"
        else:
            reason = GSC_SETUP
        out.append(self.not_checked("crawl.gsc_index_coverage", reason))
        out.append(self.not_checked("crawl.gsc_url_inspection", reason))
        return out
