"""Phase 4 data layer: authority scoring/lookup, Brave parsing + budgets, keyword engine."""
from __future__ import annotations

import asyncio
from datetime import date

import pytest

from app.config import RuntimeSettings
from app.datalayer import authority as au
from app.datalayer import keywords as ke
from app.modules.base import Status
from app.modules.m01_keywords import KeywordResearch
from app.providers.search import (
    BraveSearch, SearchBudgetExceeded, SearchResponse, SearchResult, clean_question, parse_brave,
)

from tests.unit.helpers import BASE, ctx, offline_settings, page, run, site


# ------------------------------------------------------------------ authority
def test_authority_score_and_names():
    assert [au.score(1, 1), au.score(100, 100), au.score(10_000, 10_000), au.score(1_000_000, 1_000_000)] == [100, 75, 50, 25]
    assert au.score(10, 1000) == au.score(100, 100)  # geometric mean of the two ranks
    assert au.rev_to_domain("com.facebook") == "facebook.com" and au.rev_to_domain("uk.co.bbc") == "bbc.co.uk"
    assert au.candidate_releases(date(2026, 1, 15))[:3] == ["cc-main-2025-26-nov-dec-jan", "cc-main-2025-oct-nov-dec",
                                                             "cc-main-2025-sep-oct-nov"]
    assert au.candidate_releases(date(2026, 11, 6))[1] == "cc-main-2026-aug-sep-oct"
    assert au._candidates("www.blog.example.co.uk") == ["blog.example.co.uk", "example.co.uk", "co.uk"]
    rels = ["cc-main-2026-sep-oct-nov", "cc-main-2026-oct-nov-dec", "cc-main-2025-26-nov-dec-jan", "cc-main-2025-oct-nov-dec"]
    assert sorted(rels, key=au.release_key, reverse=True) == [
        "cc-main-2026-oct-nov-dec", "cc-main-2026-sep-oct-nov", "cc-main-2025-26-nov-dec-jan", "cc-main-2025-oct-nov-dec"]


@pytest.mark.db
def test_authority_lookup_with_releases(database_url, db):
    from datetime import datetime, timedelta, timezone
    from app.db.models import CCRelease, DomainAuthority
    db.query(DomainAuthority).delete()
    db.query(CCRelease).delete()
    now = datetime.now(timezone.utc)
    db.add_all([CCRelease(release="r-old", status="ready", rows=2, top_n=1000, is_current=False, finished_at=now - timedelta(days=90)),
                CCRelease(release="r-new", status="ready", rows=2, top_n=1000, is_current=True, finished_at=now),
                DomainAuthority(release="r-new", domain="example.com", harmonic_pos=100, pagerank_pos=100, score=75),
                DomainAuthority(release="r-old", domain="example.com", harmonic_pos=400, pagerank_pos=400, score=67)])
    db.commit()
    got = au.lookup(["www.example.com", "shop.example.com", "unknown.org"])
    assert got["www.example.com"]["score"] == 75 and got["www.example.com"]["previous_score"] == 67
    assert got["shop.example.com"]["matched_domain"] == "example.com"
    assert got["unknown.org"]["score"] is None and "outside top 1,000" in got["unknown.org"]["label"]
    db.query(DomainAuthority).delete()
    db.query(CCRelease).delete()
    db.commit()


# ------------------------------------------------------------------ search provider
BRAVE = {"web": {"results": [{"url": "https://a.com/x", "title": "Go kart price list", "description": "desc",
                              "extra_snippets": ["more"]}, {"title": "no url"}]},
         "faq": {"results": [{"question": "How fast is a go kart?"}]},
         "discussions": {"results": [{"data": {"question": "Best kart for kids?"}}]}}


def test_parse_brave():
    r = parse_brave("go kart", BRAVE)
    assert [x.url for x in r.results] == ["https://a.com/x"] and "more" in r.results[0].snippet
    assert r.questions == ["How fast is a go kart?", "Best kart for kids?"]
    assert SearchResponse.from_json(r.to_json()).results[0].title == "Go kart price list"


def test_clean_question_strips_markup_and_drops_post_bodies():
    assert clean_question("<p>why isn&#39;t karting\n popular?</p>") == "why isn't karting popular?"
    assert clean_question("<p>" + "long forum post " * 20 + "</p>") is None
    assert clean_question("") is None


@pytest.mark.db
def test_brave_budget_and_cache(database_url, monkeypatch):
    import httpx
    calls = []

    class FakeClient:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url, params=None, headers=None):
            calls.append((params, headers))
            return httpx.Response(200, json=BRAVE, request=httpx.Request("GET", url))
    monkeypatch.setattr("app.providers.search.httpx.AsyncClient", FakeClient)
    s = RuntimeSettings()
    s.budgets.brave_queries_per_audit = 2
    b = BraveSearch(s, None, "key")
    asyncio.run(b.search(f"unique-q-{id(b)}-1"))
    asyncio.run(b.search(f"unique-q-{id(b)}-1"))  # cached: no new request, no budget used
    assert len(calls) == 1 and b.used_this_audit == 1 and b.cached_hits == 1
    assert calls[0][1]["X-Subscription-Token"] == "key"
    asyncio.run(b.search(f"unique-q-{id(b)}-2", country="in", lang="en-GB"))
    assert calls[-1][0]["country"] == "IN" and calls[-1][0]["search_lang"] == "en"
    with pytest.raises(SearchBudgetExceeded):
        asyncio.run(b.search(f"unique-q-{id(b)}-3"))


# ------------------------------------------------------------------ keyword engine
def R(url, title):
    return SearchResult(url=url, title=title)


def test_compute_difficulty_formula():
    results = [R(f"https://site{i}.com/buy-go-karts", "Buy Go Karts Online") for i in range(5)] + \
              [R(f"https://blog{i}.com/what-is-a-kart", "What is a kart") for i in range(5)]
    auth = {f"site{i}.com": {"score": 80} for i in range(5)}  # blogs are outside the table -> 10
    w = {"authority": 0.55, "content_depth": 0.15, "exact_match_titles": 0.15, "intent_match": 0.15}
    d = ke.compute_difficulty("buy go karts", "transactional", results, auth, [2500, 2500, 500], w)
    assert d.components == {"authority": 45.0, "content_depth": 100.0, "exact_match_titles": 50.0, "intent_match": 50.0}
    assert d.score == round(0.55 * 45 + 0.15 * 100 + 0.15 * 50 + 0.15 * 50)
    no_depth = ke.compute_difficulty("buy go karts", None, results, auth, [], w)  # missing components re-weighted
    assert set(no_depth.components) == {"authority", "exact_match_titles"}
    assert no_depth.score == round((0.55 * 45 + 0.15 * 50) / 0.70)
    assert ke.compute_difficulty("x", None, [], {}, [], w).score is None


def test_intent_of_result():
    assert ke.intent_of_result("https://x.com/products/kart", "Racing kart") == "transactional"
    assert ke.intent_of_result("https://x.com/blog/how-to-drive", "How to drive a kart") == "informational"
    assert ke.intent_of_result("https://x.com/best-karts-2026", "Best karts") == "commercial"


def test_volume_chain_falls_through():
    class Broken(ke.VolumeProvider):
        name = "Broken"
        async def volumes(self, k, c, l): raise ke.SourceUnavailable("nope")

    class Works(ke.VolumeProvider):
        name = "Works"
        async def volumes(self, k, c, l): return {x: ke.Volume(x, relative_index=50, source="Works") for x in k}
    v, notes = asyncio.run(ke.volume_chain(["a", "b"], "IN", "en", [Broken(), Works()]))
    assert v["a"].relative_index == 50 and notes == ["Broken: nope"]


class FakeSearch:
    def __init__(self):
        self.queries = []

    async def search(self, q, **kw):
        self.queries.append(q)
        return SearchResponse(query=q, results=[R("https://bigsite.com/karts", f"{q} for sale")] * 10,
                              questions=[f"how much does a {q} cost?"])


def test_keyword_engine_in_module(monkeypatch):
    monkeypatch.setattr(au, "lookup", lambda doms: {d: {"score": 60, "release": "r"} for d in doms})

    async def no_fetch(urls, timeout=15.0):
        return []
    monkeypatch.setattr(ke, "fetch_word_counts", no_fetch)
    s = site([page("/", headings=[(1, "Racing Karts")], text="We build electric go karts. How much does it cost? "
                                                         "Prices and delivery for every kart.")])
    c = ctx(s)
    c.search = FakeSearch()
    c.inputs.keywords = ["electric go kart"]
    r = run(KeywordResearch(), c)
    assert r["kw.mapping"].status == Status.NOT_CHECKED  # no LLM
    diff = r["kw.difficulty"]
    assert diff.status == Status.PASS and diff.total == 1  # owner keyword only (no H1 fallback)
    assert {i.evidence["keyword"] for i in diff.items} == {"electric go kart"}
    assert "Common Crawl r" in diff.sources[0]
    opp = r["kw.opportunities"]
    assert opp.status == Status.WARNING and any("cost" in i.evidence["keyword_idea"] for i in opp.items)
    assert r["kw.search_volume"].status == Status.NOT_CHECKED  # Trends disabled in offline tests



def test_engine_needs_keywords_without_llm():
    r = run(KeywordResearch(), ctx(site([page("/", headings=[(1, "About")])])))
    assert r["kw.search_volume"].status == Status.NOT_CHECKED and "target keywords" in r["kw.search_volume"].reason
