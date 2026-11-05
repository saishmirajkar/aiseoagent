"""Post-crawl computations on the Site Model: click depth, inlinks, sitemap membership,
page templates and duplicate clusters."""
from __future__ import annotations

from collections import defaultdict, deque

from app.crawler.simhash import hamming
from app.crawler.urls import template_key
from app.sitemodel.types import SiteModel

MIN_WORDS_FOR_DUPES = 20
NEAR_DUPLICATE_HAMMING = 3  # of 64 simhash bits


def _resolve(model: SiteModel, url: str) -> str:
    """Follow a crawled redirect to its final internal page, if known."""
    p = model.pages.get(url)
    if p is not None and p.redirect_chain:
        final = p.final_url
        if final in model.pages:
            return final
    return url


def build_model(model: SiteModel) -> SiteModel:
    pages = model.pages

    # inbound internal links (unique source pages, self-links excluded, redirects resolved)
    sources: dict[str, set[str]] = defaultdict(set)
    graph: dict[str, set[str]] = defaultdict(set)
    for p in pages.values():
        if not p.is_ok:
            continue
        for l in p.links:
            if not l.internal:
                continue
            for t in {l.target, _resolve(model, l.target)}:
                if t != p.url:
                    sources[t].add(p.url)
                    graph[p.url].add(t)
    for url, p in pages.items():
        srcs = sources.get(url, set())
        p.inlinks = len(srcs)
        p.inlink_sources = sorted(srcs)[:20]
        p.in_sitemap = url in model.sitemap_urls
        p.template_key = template_key(url)

    # shortest click depth from the start page
    home = model.facts.start_url
    for p in pages.values():
        p.depth = None
    if home in pages:
        dist = {home: 0}
        dq = deque([home])
        while dq:
            u = dq.popleft()
            for v in graph.get(u, ()):
                if v not in dist:
                    dist[v] = dist[u] + 1
                    dq.append(v)
        for url, d in dist.items():
            if url in pages:
                pages[url].depth = d
        # a start URL that redirected: its target is the homepage too
        hp = pages[home]
        if hp.redirect_chain and hp.final_url in pages:
            pages[hp.final_url].depth = 0

    # duplicates: exact content hash, then simhash near-duplicates
    cands = [p for p in pages.values() if p.is_ok and p.word_count >= MIN_WORDS_FOR_DUPES and p.simhash is not None]
    by_hash: dict[str, list[str]] = defaultdict(list)
    for p in cands:
        by_hash[p.content_hash].append(p.url)
    groups = [sorted(g) for g in by_hash.values() if len(g) > 1]
    grouped = {u for g in groups for u in g}
    reps = [p for p in cands if p.url not in grouped or by_hash[p.content_hash][0] == p.url]
    parent = {p.url: p.url for p in reps}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    thr = NEAR_DUPLICATE_HAMMING
    for i in range(len(reps)):
        for j in range(i + 1, len(reps)):
            a, b = reps[i], reps[j]
            if a.content_hash == b.content_hash:
                continue
            if hamming(a.simhash, b.simhash) <= thr:
                parent[find(a.url)] = find(b.url)
    near: dict[str, list[str]] = defaultdict(list)
    for p in reps:
        near[find(p.url)].append(p.url)
    exact_sets = [set(g) for g in groups]
    for g in near.values():
        if len(g) > 1:
            merged = set(g)
            for es in exact_sets:
                if es & merged:
                    merged |= es
            groups = [x for x in groups if not (set(x) & merged)]
            groups.append(sorted(merged))
    model.duplicate_groups = sorted(groups, key=len, reverse=True)
    return model
