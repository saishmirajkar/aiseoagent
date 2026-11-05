"""Module 6: Technical Audit."""
from __future__ import annotations

from collections import defaultdict

from app.modules.base import (
    AuditContext, Category, CheckModule, CheckResult, CheckSpec, FindingItem, Level, Need, Status,
)
from app.modules.common import (
    cached_link_sources, esc_attr, html_tag_fix, internal_targets, is_important, redirect_fix,
)

H, M, L = Level.HIGH, Level.MED, Level.LOW
UNVERIFIABLE = {401, 403, 429, 999}  # bot protection: cannot conclude the link is broken


def _broken(status: int | None, error: str | None) -> bool:
    if status is not None:
        return status in (404, 410) or status >= 500 or (400 <= status < 500 and status not in UNVERIFIABLE)
    return bool(error) and ("ConnectError" in error or "Name or service" in error or "nodename" in error)


class TechnicalAudit(CheckModule):
    key = "technical"
    number = 6
    name = "Technical Audit"
    category = Category.TECHNICAL
    needs = frozenset({Need.PAGES, Need.LINKS, Need.SITE_FACTS})
    checks = (
        CheckSpec("tech.status_4xx", "Pages returning 4xx errors", H, L,
                  "Search engines drop 4xx URLs from the index, and visitors who land on them leave.",
                  "Restore the page, 301-redirect it to the closest live page, or remove the links pointing to it.",
                  population="pages"),
        CheckSpec("tech.status_5xx", "Pages returning 5xx server errors", H, M,
                  "Server errors stop crawling; repeated 5xx responses lead to pages being dropped.",
                  "Check server logs for these URLs and fix the application or hosting error.",
                  population="pages"),
        CheckSpec("tech.fetch_errors", "Pages that timed out or failed to load", M, M,
                  "Pages that time out cannot be crawled or indexed reliably.",
                  "Check server performance and firewall/bot-protection rules for these URLs.",
                  population="pages"),
        CheckSpec("tech.broken_internal_links", "Broken internal links", H, L,
                  "Broken links waste crawl budget, leak link equity and frustrate visitors.",
                  "Update each link to point at a live URL (or remove it).", population="pages",
                  missing_label=None),
        CheckSpec("tech.broken_external_links", "Broken external links", L, L,
                  "Links to dead external pages hurt user trust and perceived page quality.",
                  "Replace with a working source or remove the link.", population="links"),
        CheckSpec("tech.redirect_chains", "Redirect chains (more than one hop)", M, L,
                  "Each extra hop slows users and crawlers; long chains may not be followed.",
                  "Point the first URL straight at the final destination with a single 301.",
                  population="pages"),
        CheckSpec("tech.redirect_loops", "Redirect loops", H, L,
                  "A loop makes the URL unreachable for users and search engines.",
                  "Fix the redirect rules so the URL resolves to a 200 page.", population="pages"),
        CheckSpec("tech.links_to_redirects", "Internal links pointing to redirects", L, L,
                  "Linking to redirecting URLs adds latency and dilutes signals.",
                  "Update internal links to the final destination URL.", population="pages"),
        CheckSpec("tech.canonical_missing", "Missing canonical tag", L, L,
                  "Without a canonical, search engines guess the preferred URL among duplicates.",
                  "Add a self-referencing <link rel=\"canonical\"> to every indexable page.",
                  population="pages", missing_label="Canonical tag missing on {affected} page(s)"),
        CheckSpec("tech.canonical_broken", "Canonical points to a non-200 URL", H, L,
                  "A canonical to an error or redirect is ignored and confuses indexing.",
                  "Point the canonical at the live, final (200) version of the page.", population="pages"),
        CheckSpec("tech.canonical_conflict", "Conflicting canonical signals", M, L,
                  "Multiple or contradictory canonicals make search engines ignore all of them.",
                  "Keep exactly one canonical in <head>, consistent between HTML, HTTP header and rendered DOM.",
                  population="pages"),
        CheckSpec("tech.noindex_important", "Noindex on important pages", H, L,
                  "Important pages with noindex cannot appear in search results.",
                  "Remove the noindex directive (meta robots or X-Robots-Tag) if the page should rank.",
                  population="pages"),
        CheckSpec("tech.hreflang", "Hreflang errors", M, M,
                  "Invalid hreflang annotations are ignored, so the wrong language version may rank.",
                  "Use valid ISO codes, include a self-reference, make every alternate link back, and point only at 200 canonical URLs.",
                  population="pages"),
        CheckSpec("tech.https", "HTTPS and HTTP→HTTPS redirect", H, M,
                  "HTTPS is a ranking signal and browsers flag HTTP pages as 'Not secure'.",
                  "Serve the site over HTTPS and 301-redirect every HTTP URL to its HTTPS version."),
        CheckSpec("tech.mixed_content", "Mixed content on HTTPS pages", M, L,
                  "HTTP resources on HTTPS pages are blocked or flagged by browsers.",
                  "Load every resource over HTTPS (update URLs or use protocol-relative/HTTPS sources).",
                  population="pages"),
        CheckSpec("tech.url_variants", "Duplicate URL variants (www / trailing slash)", M, L,
                  "When several URL variants return 200, signals split across duplicates.",
                  "301-redirect all variants to one preferred host and URL format."),
        CheckSpec("tech.viewport", "Missing or incorrect viewport meta tag", H, L,
                  "Without a responsive viewport the page fails mobile-friendliness (mobile-first indexing).",
                  "Add <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">.",
                  population="pages", missing_label="Viewport meta tag missing on {affected} page(s)"),
        CheckSpec("tech.lang", "Missing html lang attribute", L, L,
                  "The lang attribute helps search engines and screen readers identify the language.",
                  "Add lang to the <html> element, e.g. <html lang=\"en\">.", population="pages",
                  missing_label="html lang attribute missing on {affected} page(s)"),
        CheckSpec("tech.slow_response", "Slow server response", M, M,
                  "Slow responses hurt crawl rate and Largest Contentful Paint.",
                  "Add caching (page/CDN), optimize backend queries, or upgrade hosting.", population="pages"),
        CheckSpec("tech.js_dependent", "Content that only appears after JavaScript", M, H,
                  "Content and links that need JavaScript are indexed later and less reliably.",
                  "Server-side render or pre-render primary content and navigation links.", population="pages"),
        CheckSpec("tech.render_mismatch", "Raw vs rendered HTML disagree on key tags", M, M,
                  "If JavaScript changes the title, canonical or robots tags, search engines may use either version.",
                  "Output the final title, canonical and robots directives in the server HTML.",
                  population="pages"),
        CheckSpec("tech.meta_refresh", "Meta refresh redirects", L, L,
                  "Meta refresh is a slow, weak redirect compared with a server-side 301.",
                  "Replace meta refresh with a server-side 301 redirect.", population="pages"),
    )

    async def run(self, ctx: AuditContext) -> list[CheckResult]:
        site = ctx.site
        th = ctx.settings.thresholds
        pages = list(site.pages.values())
        ok = site.html_pages()
        sources = cached_link_sources(ctx)
        internal = internal_targets(ctx)
        internal_known = len(pages) + sum(1 for u in site.url_status if u in internal)
        out: list[CheckResult] = []

        def status_items(pred) -> list[FindingItem]:
            items = []
            for p in pages:
                if p.status is not None and pred(p.status) and not p.redirect_chain:
                    items.append(FindingItem(p.url, {"status": p.status, "linked_from": sources.get(p.url, [])[:5],
                                                     "in_sitemap": p.in_sitemap}))
            for u, s in site.url_status.items():
                if u in internal and s.status is not None and pred(s.status) and not s.redirects:
                    items.append(FindingItem(u, {"status": s.status, "linked_from": sources[u][:5],
                                                 "crawled": False}))
            return items

        out.append(self.by_population("tech.status_4xx", status_items(lambda s: 400 <= s < 500),
                                      internal_known, summary="Internal URLs return client errors"))
        out.append(self.by_population("tech.status_5xx", status_items(lambda s: s >= 500),
                                      internal_known, summary="Internal URLs return server errors"))
        errs = [FindingItem(p.url, {"error": p.error, "linked_from": sources.get(p.url, [])[:5]})
                for p in pages if p.status is None and p.error and not p.blocked_by_robots]
        out.append(self.by_population("tech.fetch_errors", errs, len(pages), fail=False,
                                      summary="Pages could not be fetched"))

        # broken links (grouped per source page)
        broken_by_page: dict[str, list[dict]] = defaultdict(list)
        ext_items: list[FindingItem] = []
        ext_total = 0
        for target, srcs in sources.items():
            st = site.status_of(target)
            if st is None:
                continue
            final = site.final_status_of(target)
            if target in internal:
                if st.redirects:
                    continue  # handled by redirect checks; final status checked below
                if _broken(final, st.error):
                    for s in srcs:
                        broken_by_page[s].append({"link": target, "status": final, "error": st.error})
            else:
                ext_total += 1
                if _broken(final if final is not None else st.status, st.error):
                    ext_items.append(FindingItem(srcs[0], {"link": target, "status": st.status,
                                                           "error": st.error, "linked_from": srcs[:5]}))
        bil = [FindingItem(src, {"broken_links": v[:20], "count": len(v)}) for src, v in broken_by_page.items()]
        out.append(self.by_population("tech.broken_internal_links", bil, len(ok),
                                      summary=f"{sum(len(v) for v in broken_by_page.values())} broken internal link(s) "
                                              f"on {len(bil)} page(s)"))
        r = self.by_population("tech.broken_external_links", ext_items, ext_total, fail=False,
                               summary=f"{len(ext_items)} external link(s) return errors")
        if r.status == Status.PASS and ext_total:
            r.summary = f"{ext_total} external link(s) checked; none broken (bot-protected 401/403/429 not counted)"
        out.append(r)

        # redirects
        chains, loops, link_redirects = [], [], defaultdict(list)
        for p in pages:
            if p.redirect_loop:
                loops.append(FindingItem(p.url, {"chain": p.redirect_chain}))
            elif len(p.redirect_chain) > 1:
                chains.append(FindingItem(p.url, {"hops": len(p.redirect_chain), "chain": p.redirect_chain,
                                                  "final_url": p.final_url},
                                          generated_fix=redirect_fix(p.url, p.final_url)))
        for u, s in site.url_status.items():
            if s.redirects > 1 and u in sources:
                chains.append(FindingItem(u, {"hops": s.redirects, "final_url": s.final_url},
                                          generated_fix=redirect_fix(u, s.final_url or "")))
        redirecting = {p.url: p.final_url for p in pages if p.redirect_chain and not p.redirect_loop}
        redirecting.update({u: s.final_url for u, s in site.url_status.items() if s.redirects})
        for p in ok:
            for l in p.links:
                if l.internal and l.target in redirecting:
                    link_redirects[p.url].append({"link": l.target, "redirects_to": redirecting[l.target]})
        if redirecting:
            out.append(self.by_population("tech.redirect_chains", chains, len(redirecting), fail=False,
                                          summary=f"{len(chains)} URL(s) redirect through 2+ hops"))
        else:
            out.append(self.result("tech.redirect_chains", Status.PASS, "No internal redirects found"))
        out.append(self.by_population("tech.redirect_loops", loops, len(pages), summary="Redirect loops found"))
        lr_items = [FindingItem(u, {"links": v[:20], "count": len(v)}) for u, v in link_redirects.items()]
        out.append(self.by_population("tech.links_to_redirects", lr_items, len(ok), fail=False,
                                      summary=f"{sum(len(v) for v in link_redirects.values())} internal link(s) "
                                              f"point at redirecting URLs"))

        # canonicals
        indexable_candidates = [p for p in ok if not p.is_noindex]
        missing = [FindingItem(p.url, {"canonical": "(missing)"},
                               generated_fix=html_tag_fix(f'<link rel="canonical" href="{esc_attr(p.url)}">'))
                   for p in indexable_candidates if not p.canonical_target]
        out.append(self.by_population("tech.canonical_missing", missing, len(indexable_candidates), fail=False,
                                      summary=f"{len(missing)} page(s) have no canonical tag"))
        broken_c, conflict = [], []
        for p in ok:
            c = p.canonical_target
            if c and c != p.url:
                st = site.status_of(c)
                if st is not None and (st.status != 200 or st.redirects):
                    ev = {"canonical": c, "canonical_status": st.status, "canonical_redirects": st.redirects}
                    fixed = st.final_url if st.redirects and site.final_status_of(c) == 200 else p.url
                    broken_c.append(FindingItem(p.url, ev, generated_fix=html_tag_fix(
                        f'<link rel="canonical" href="{esc_attr(fixed)}">')))
                elif st is not None and c in site.pages and site.pages[c].is_noindex:
                    conflict.append(FindingItem(p.url, {"problem": "Canonical target is noindex", "canonical": c}))
            reasons = []
            if len(set(p.canonicals)) > 1:
                reasons.append({"problem": "Multiple different canonical tags", "values": sorted(set(p.canonicals))})
            if p.canonical and p.canonical_header and p.canonical != p.canonical_header:
                reasons.append({"problem": "HTTP Link header and HTML canonical differ",
                                "html": p.canonical, "header": p.canonical_header})
            if p.canonical_in_body:
                reasons.append({"problem": "Canonical tag placed in <body> (ignored by Google)"})
            if p.is_noindex and p.canonical and p.canonical != p.url:
                reasons.append({"problem": "Page is noindex and canonicalised elsewhere (mixed signals)",
                                "canonical": p.canonical})
            for r in reasons:
                conflict.append(FindingItem(p.url, r))
        out.append(self.by_population("tech.canonical_broken", broken_c, len(ok),
                                      summary="Canonical tags point at URLs that do not return 200"))
        out.append(self.by_population("tech.canonical_conflict", conflict, len(ok),
                                      summary="Pages send contradictory canonical signals"))

        # noindex on important pages
        noidx = [FindingItem(p.url, {"meta_robots": p.meta_robots, "x_robots_tag": p.x_robots_tag,
                                     "depth": p.depth, "inlinks": p.inlinks, "in_sitemap": p.in_sitemap})
                 for p in ok if p.is_noindex and is_important(p, site, th.important_page_min_inlinks)]
        out.append(self.by_population("tech.noindex_important", noidx, len(ok),
                                      summary="Important pages are set to noindex"))

        out.append(self._hreflang(ctx, ok))
        out.append(self._https(ctx))
        mixed = [FindingItem(p.url, {"resources": p.mixed_content[:20], "count": len(p.mixed_content)})
                 for p in ok if p.mixed_content]
        https_pages = [p for p in ok if p.url.startswith("https://")]
        out.append(self.by_population("tech.mixed_content", mixed, len(https_pages),
                                      summary="HTTPS pages load HTTP resources") if https_pages
                   else self.not_applicable("tech.mixed_content", "Site is not served over HTTPS (see HTTPS check)"))
        out.append(self._variants(ctx))

        vp_tag = '<meta name="viewport" content="width=device-width, initial-scale=1">'
        vp = []
        for p in ok:
            if not p.viewport:
                vp.append(FindingItem(p.url, {"viewport": "(missing)"}, generated_fix=html_tag_fix(vp_tag)))
            elif "width=device-width" not in p.viewport.replace(" ", "").lower():
                vp.append(FindingItem(p.url, {"viewport": p.viewport, "problem": "no width=device-width"},
                                      generated_fix=html_tag_fix(vp_tag)))
        out.append(self.by_population("tech.viewport", vp, len(ok), summary="Pages lack a responsive viewport"))
        lang = [FindingItem(p.url, {"lang": "(missing)"}, generated_fix=html_tag_fix('<html lang="en">', "<html> element"))
                for p in ok if not p.lang]
        out.append(self.by_population("tech.lang", lang, len(ok), fail=False, summary="Pages lack html lang"))

        slow = [FindingItem(p.url, {"response_ms": p.response_ms})
                for p in ok if p.response_ms and p.response_ms > th.slow_response_ms]
        r = self.by_population("tech.slow_response", sorted(slow, key=lambda i: -i.evidence["response_ms"]), len(ok),
                               fail=False, summary=f"Responses slower than {th.slow_response_ms} ms (time to headers)")
        r.sources = ["Crawl (single request from the audit server; not field data)"]
        out.append(r)

        renderer_err = site.facts.crawl.get("renderer_error")
        rendered = [p for p in ok if p.rendered]
        if not rendered:
            reason = renderer_err or ("JavaScript rendering is disabled in Settings"
                                      if ctx.settings.crawl.render_mode == "never" else "No pages were rendered")
            out.append(self.not_checked("tech.js_dependent", reason))
            out.append(self.not_checked("tech.render_mismatch", reason))
        else:
            jsd = [FindingItem(p.url, {"raw_words": (p.raw_signals or {}).get("word_count"),
                                       "rendered_words": p.word_count,
                                       "raw_internal_links": (p.raw_signals or {}).get("internal_links"),
                                       "rendered_internal_links": sum(1 for l in p.links if l.internal)})
                   for p in rendered if p.js_dependent]
            r = self.by_population("tech.js_dependent", jsd, len(rendered), fail=False,
                                   summary="Main content or links only appear after JavaScript runs")
            r.sources = ["Crawl (raw HTML vs headless Chromium render)"]
            out.append(r)
            mm = []
            for p in rendered:
                raw = p.raw_signals or {}
                diffs = {k: {"raw": raw.get(k), "rendered": getattr(p, k)}
                         for k in ("title", "canonical", "meta_robots")
                         if (raw.get(k) or None) != (getattr(p, k) or None)}
                if diffs:
                    mm.append(FindingItem(p.url, diffs))
            out.append(self.by_population("tech.render_mismatch", mm, len(rendered),
                                          summary="JavaScript changes title/canonical/robots"))
        mr = [FindingItem(p.url, {"meta_refresh": p.meta_refresh}) for p in ok if p.meta_refresh]
        out.append(self.by_population("tech.meta_refresh", mr, len(ok), fail=False, summary="Meta refresh used"))
        return out

    # ------------------------------------------------------------------ sub-checks
    def _hreflang(self, ctx: AuditContext, ok) -> CheckResult:
        site = ctx.site
        with_h = [p for p in ok if p.hreflang]
        if not with_h:
            return self.not_applicable("tech.hreflang", "No hreflang annotations found (single-language site)")
        items = []
        for p in with_h:
            problems = []
            hrefs = {h["href"] for h in p.hreflang if h.get("href")}
            invalid = [h["lang"] for h in p.hreflang if not h.get("valid_code")]
            if invalid:
                problems.append({"problem": "Invalid language/region codes", "codes": invalid})
            if p.url not in hrefs:
                problems.append({"problem": "No self-referencing hreflang"})
            langs = [h["lang"].lower() for h in p.hreflang]
            dup = sorted({x for x in langs if langs.count(x) > 1})
            if dup:
                problems.append({"problem": "Same language code points to several URLs", "codes": dup})
            if "x-default" not in langs:
                problems.append({"problem": "No x-default alternate (recommended)", "severity": "info"})
            for h in p.hreflang:
                href = h.get("href")
                if not href or href == p.url:
                    continue
                st = site.status_of(href)
                if st is not None and (st.status != 200 or st.redirects):
                    problems.append({"problem": "Alternate URL does not return 200", "href": href,
                                     "status": st.status, "redirects": st.redirects})
                    continue
                alt = site.pages.get(href)
                if alt is not None and alt.is_ok:
                    if p.url not in {x.get("href") for x in alt.hreflang}:
                        problems.append({"problem": "Missing return link", "href": href})
                    if alt.canonical_target and alt.canonical_target != href:
                        problems.append({"problem": "Alternate URL is canonicalised elsewhere", "href": href,
                                         "canonical": alt.canonical_target})
            real = [x for x in problems if x.get("severity") != "info"]
            if real:
                items.append(FindingItem(p.url, {"problems": problems}))
        return self.by_population("tech.hreflang", items, len(with_h), summary="Hreflang annotations have errors")

    def _https(self, ctx: AuditContext) -> CheckResult:
        h = ctx.site.facts.https
        if h.get("start_scheme") == "https":
            if h.get("http_redirects_to_https"):
                return self.result("tech.https", Status.PASS, "Site uses HTTPS and redirects HTTP to HTTPS",
                                   items=[FindingItem(None, {"hsts": h.get("hsts") or "not set"})])
            probe = h.get("http_probe") or {}
            if probe.get("status") is None:
                return self.result("tech.https", Status.PASS, "Site uses HTTPS; plain HTTP is not served",
                                   items=[FindingItem(None, {"http_probe": probe})])
            return self.result("tech.https", Status.FAIL, "HTTP version does not redirect to HTTPS",
                               items=[FindingItem(ctx.site.facts.origin.replace("https://", "http://") + "/",
                                                  {"http_probe": probe},
                                                  generated_fix={"type": "redirect_rule",
                                                                 "apache": "RewriteEngine On\nRewriteCond %{HTTPS} off\n"
                                                                           "RewriteRule ^ https://%{HTTP_HOST}%{REQUEST_URI} [L,R=301]",
                                                                 "nginx": "server { listen 80; server_name _; "
                                                                          "return 301 https://$host$request_uri; }"})],
                               affected=1)
        return self.result("tech.https", Status.FAIL, "Site is served over plain HTTP",
                           items=[FindingItem(ctx.site.facts.start_url,
                                              {"https_available": h.get("https_available"),
                                               "https_probe": h.get("https_probe"),
                                               "https_error": h.get("https_error")})],
                           affected=1)

    def _variants(self, ctx: AuditContext) -> CheckResult:
        v = ctx.site.facts.variants
        items = []
        status = Status.PASS
        www = v.get("www") or {}
        if www.get("resolves") and not www.get("consolidated"):
            status = Status.FAIL
            items.append(FindingItem(www.get("variant"), {"problem": "Both www and non-www versions return 200",
                                                          "status": www.get("status"), "final_url": www.get("final_url")},
                                     generated_fix=redirect_fix(www.get("variant") or "", ctx.site.facts.start_url)))
        ts = v.get("trailing_slash")
        if ts and ts.get("duplicate"):
            if status == Status.PASS:
                status = Status.WARNING
            items.append(FindingItem(ts.get("variant"), {
                "problem": "Trailing-slash variant returns 200 instead of redirecting",
                "original": ts.get("page"), "original_canonical": ts.get("page_canonical")},
                generated_fix=redirect_fix(ts.get("variant") or "", ts.get("page") or "")))
        if status == Status.PASS:
            return self.result("tech.url_variants", Status.PASS, "URL variants consolidate to one version",
                               items=[FindingItem(None, v)])
        return self.result("tech.url_variants", status, "Duplicate URL variants are reachable", items=items,
                           affected=len(items))
