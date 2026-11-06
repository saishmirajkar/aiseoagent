"""Modules 10 (backlinks), 11 (outreach), 12 (mentions), 16 (AI visibility) + inlink import API."""
from __future__ import annotations

import pytest

from app.datalayer import authority
from app.datalayer.backlinks import Backlink, Discovery, Mention, suspicious_patterns
from app.llm.client import Answer, BudgetExceeded
from app.modules import m10_backlinks, m11_outreach
from app.modules.base import Status
from app.modules.m10_backlinks import BacklinkAnalysis
from app.modules.m11_outreach import LinkBuilding, public_emails
from app.modules.m12_mentions import BrandMentions
from app.modules.m16_ai_visibility import AIVisibility, mentioned
from app.providers.search import SearchResponse, SearchResult

from tests.unit.helpers import BASE, ctx, page, run, site


class FakeSearch:
    def __init__(self, results_for=None):
        self.queries = []
        self.results_for = results_for or (lambda q: [])

    async def search(self, q, **kw):
        self.queries.append(q)
        return SearchResponse(query=q, results=self.results_for(q))


class FakeLLM:
    def __init__(self, json_resp=None, answers=None):
        self.json_resp = json_resp or {}
        self.answers = answers
        self.calls = []

    async def json(self, *, task, **kw):
        self.calls.append(task)
        r = self.json_resp.get(task)
        return r(kw) if callable(r) else r

    async def answer(self, *, task, prompt, **kw):
        self.calls.append(task)
        return self.answers(prompt)


def _site():
    s = site([page("/", title="Acme Karts | Home")])
    s.facts.host = "acme.com"
    s.facts.origin = "https://acme.com"
    return s


def _disc():
    d = Discovery(target_domain="acme.com", queries=['"acme.com" -site:acme.com'], candidates=12, pages_checked=8)
    d.backlinks = [Backlink("https://news.com/a", "news.com", "https://acme.com/", "Acme Karts", False, False, False, 70, 12),
                   Backlink("https://farm.xyz/p", "farm.xyz", "https://acme.com/x", "karts", True, False, False, None, 450)]
    d.mentions = [Mention("https://blog.org/karting", "blog.org", "Karting in Pune", "Acme makes great karts", 35)]
    return d


@pytest.fixture
def no_db(monkeypatch):
    monkeypatch.setattr(authority, "lookup", lambda doms: {d: {"score": 50, "release": "r1", "previous_score": 52}
                                                           for d in doms})
    monkeypatch.setattr(m10_backlinks.inlinks, "referring_domains", lambda d: None)


# ------------------------------------------------------------------ module 10
def test_backlinks_module(monkeypatch, no_db):
    async def fake_discover(target, brand, search, settings, **kw):
        return _disc() if target == "acme.com" else Discovery(target, backlinks=[
            Backlink("https://news.com/z", "news.com", "x", "", False, False, False, 70),
            Backlink("https://mag.com/z", "mag.com", "x", "", False, False, False, 60)], queries=["q"])
    monkeypatch.setattr(m10_backlinks, "discover", fake_discover)
    c = ctx(_site())
    c.search = FakeSearch()
    c.inputs.brand = "Acme"
    c.inputs.competitors = ["https://rival.com/"]
    r = run(BacklinkAnalysis(), c)
    assert r["bl.authority"].status == Status.PASS and "50/100" in r["bl.authority"].summary
    disc = r["bl.discovered"]
    assert disc.status == Status.PASS and "sample" in disc.summary and len(disc.items) == 2
    assert disc.items[1].evidence["rel"] == "nofollow"
    rd = r["bl.referring_domains"]
    assert rd.status == Status.NOT_CHECKED
    assert rd.reason.startswith("Full referring-domain list not available; run the Colab import")
    gap = r["bl.link_gap"]
    assert gap.status == Status.WARNING and [i.evidence["domain"] for i in gap.items] == ["mag.com"]  # news.com already links
    assert c.shared["link_gap"][0]["links_to"] == ["rival.com"]


def test_backlinks_with_imported_list(monkeypatch, no_db):
    monkeypatch.setattr(m10_backlinks.inlinks, "referring_domains", lambda d: {
        "release": "r2", "domains": {"a.com", "b.com", "c.com"}, "previous_release": "r1",
        "previous_domains": {"a.com", "old.com"}})

    async def fake_discover(*a, **k):
        return _disc()
    monkeypatch.setattr(m10_backlinks, "discover", fake_discover)
    c = ctx(_site())
    c.search = FakeSearch()
    r = run(BacklinkAnalysis(), c)
    rd = r["bl.referring_domains"]
    assert rd.status == Status.WARNING and "2 new, 1 lost" in rd.summary
    assert rd.items[1].evidence == {"lost_domain": "old.com"}


def test_backlinks_without_search(no_db):
    r = run(BacklinkAnalysis(), ctx(_site()))
    assert r["bl.discovered"].status == Status.NOT_CHECKED and "BRAVE_API_KEY" in r["bl.discovered"].reason
    assert r["bl.link_gap"].status == Status.NOT_APPLICABLE


def test_suspicious_patterns():
    doms = {f"spam{i}.xyz": None for i in range(6)} | {"good.com": 80}
    flags = {f["pattern"] for f in suspicious_patterns(doms, outbound={"farm.xyz": 450}, new_vs_previous=(120, 30))}
    assert flags == {"High share of low-authority referrers", "Cluster of spam-prone TLDs",
                     "Referrers that link to many unrelated sites", "Sudden spike in new referring domains"}
    assert suspicious_patterns({"a.com": 80, "b.com": 70, "c.com": 60, "d.com": 50, "e.com": 40}) == []


# ------------------------------------------------------------------ module 12
def test_mentions_module(no_db):
    def results(q):
        n = 9 if "rival" in q else 4
        return [SearchResult(f"https://site{i}.com/p", f"Acme review {i}", "Acme karts are great") for i in range(n)]
    c = ctx(_site())
    c.search = FakeSearch(results)
    c.inputs.brand = "Acme"
    c.inputs.competitors = ["https://rival.com/"]
    c.shared["discovery"] = _disc()
    c.llm = FakeLLM({"mentions.reviews": {"sentiment": "mixed", "summary": "Good karts, slow delivery",
                                          "positive_themes": ["build quality"], "negative_themes": ["delivery time"]}})
    r = run(BrandMentions(), c)
    assert r["mentions.unlinked"].status == Status.WARNING and r["mentions.unlinked"].items[0].url == "https://blog.org/karting"
    share = r["mentions.share"]
    assert share.status == Status.WARNING and "behind rival" in share.summary
    rev = r["mentions.reviews"]
    assert rev.status == Status.WARNING and "delivery time" in rev.items[0].evidence["negative_themes"]


# ------------------------------------------------------------------ module 11
def test_outreach_module(monkeypatch, no_db):
    saved = {}

    async def no_contacts(self, ctx, prospects):
        prospects[0]["emails"] = ["editor@news.org"]
    monkeypatch.setattr(LinkBuilding, "_contacts", no_contacts)
    monkeypatch.setattr(LinkBuilding, "_save", staticmethod(lambda site, ps, aid: saved.update(site=site, ps=ps)))

    def results(q):
        return [SearchResult("https://kartblog.org/write-for-us", "Write for us – karting blog", "karting guest post"),
                SearchResult("https://www.facebook.com/x", "fb", "karting"),
                SearchResult("https://rival.com/blog", "rival", "karting")]
    c = ctx(_site())
    c.search = FakeSearch(results)
    c.inputs.business_type = "karting"
    c.inputs.competitors = ["https://rival.com/"]
    c.shared["discovery"] = _disc()
    c.shared["link_gap"] = [{"domain": "mag.com", "links_to": ["rival.com"], "authority": 60}]
    c.llm = FakeLLM({"outreach.drafts": lambda kw: {"drafts": [{"id": i, "subject": "Hello", "body": "Hi there"}
                                                                for i in range(5)]}})
    r = run(LinkBuilding(), c)
    doms = [i.evidence["domain"] for i in r["out.prospects"].items]
    assert doms[0] == "blog.org"  # unlinked mention ranks first
    assert set(doms) == {"blog.org", "mag.com", "kartblog.org"}  # social sites and competitors excluded
    assert r["out.drafts"].status == Status.PASS and "nothing is sent" in r["out.drafts"].summary
    assert saved["site"] == "acme.com" and all(p.get("draft") for p in saved["ps"])


def test_public_emails():
    assert public_emails("contact: editor@news.org or a@b.png", ["mailto:hello@news.org?subject=x"], "news.org") == \
        ["hello@news.org", "editor@news.org"]


# ------------------------------------------------------------------ module 16
def test_ai_visibility_module():
    s = _site()
    s.facts.ai_bots = {"GPTBot": {"root_allowed": False, "has_specific_group": True, "disallow": ["/"]},
                       "ClaudeBot": {"root_allowed": True, "has_specific_group": False, "disallow": []}}
    s.facts.robots = {"url": "https://acme.com/robots.txt"}
    s.facts.llms_txt = {"present": False, "status": 404}
    prompts = ["best go kart manufacturers in india", "acme karts price", "how fast is a racing kart"]

    def answers(prompt):
        if "manufacturers" in prompt:
            return Answer("Top makers include Acme and Rival.", ["https://acme.com/about"], 1, "claude-sonnet-5-5")
        return Answer("Karts reach 100 km/h.", [], 0, "claude-sonnet-5-5")
    c = ctx(s)
    c.inputs.brand = "Acme"
    c.inputs.competitors = ["https://rival.com/"]
    c.llm = FakeLLM({"ai_visibility.prompts": {"prompts": prompts}}, answers)
    r = run(AIVisibility(), c)
    assert r["ai.crawler_access"].status == Status.WARNING and "GPTBot" in r["ai.crawler_access"].summary
    assert "User-agent: GPTBot" in r["ai.crawler_access"].generated_fix["code"]
    assert r["ai.llms_txt"].status == Status.WARNING and r["ai.llms_txt"].generated_fix["content"].startswith("# Acme")
    vis = r["ai.visibility"]
    assert len(vis.items) == 2  # the prompt containing the brand name was dropped
    assert vis.status == Status.PASS and "Brand mentioned in 50%" in vis.summary and "rival 50%" in vis.summary
    assert "sample" in vis.summary.lower() and any("not configured" in s for s in vis.sources)


def test_ai_visibility_budget_stop():
    c = ctx(_site())
    c.inputs.brand = "Acme"

    def boom(prompt):
        raise BudgetExceeded("LLM budget for this audit ($2.00) would be exceeded")
    c.llm = FakeLLM({"ai_visibility.prompts": {"prompts": ["q1", "q2"]}}, boom)
    r = run(AIVisibility(), c)
    assert r["ai.visibility"].status == Status.NOT_CHECKED and "budget" in r["ai.visibility"].reason


def test_mentioned_word_boundaries():
    assert mentioned("We love ACME karts", ["Acme"]) and not mentioned("Acmeville is nice", ["Acme"])


# ------------------------------------------------------------------ import API + store (Postgres)
@pytest.mark.db
def test_inlink_import_api(database_url, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "s3cret")
    monkeypatch.setenv("SESSION_SECRET", "x" * 32)
    monkeypatch.setenv("IMPORT_API_TOKEN", "tok-123")
    from fastapi.testclient import TestClient

    from app.config import env
    from app.datalayer import inlinks
    env.cache_clear()
    from app.main import create_app
    with TestClient(create_app()) as cl:
        body = {"release": "cc-main-2026-aug-sep-oct", "target_domain": "www.Acme-Test.com",
                "source_domains": ["a.com", "B.com", "a.com", "acme-test.com"]}
        assert cl.post("/api/import/inlinks", json=body).status_code == 401
        assert cl.post("/api/import/inlinks", json=body, headers={"Authorization": "Bearer nope"}).status_code == 401
        r = cl.post("/api/import/inlinks", json=body, headers={"Authorization": "Bearer tok-123"})
        assert r.status_code == 200 and r.json()["stored"] == 2  # deduped, self-links dropped
        bad = cl.post("/api/import/inlinks", json={**body, "release": "../etc"}, headers={"Authorization": "Bearer tok-123"})
        assert bad.status_code == 422
        for rel, srcs in (("cc-main-2026-sep-oct-nov", ["a.com", "c.com"]), ("cc-main-2026-oct-nov-dec", ["c.com"])):
            cl.post("/api/import/inlinks", json={**body, "release": rel, "source_domains": srcs},
                    headers={"Authorization": "Bearer tok-123"})
    got = inlinks.referring_domains("acme-test.com")
    assert got["release"] == "cc-main-2026-oct-nov-dec" and got["domains"] == {"c.com"}
    assert got["previous_release"] == "cc-main-2026-sep-oct-nov"  # only two releases kept
    env.cache_clear()


# ------------------------------------------------------------------ Gemini (Vertex AI)
def test_gemini_parse_and_cost(monkeypatch):
    import asyncio
    from app.providers import gemini as gm
    recorded = []
    monkeypatch.setattr(gm.usage, "record", lambda *a: recorded.append(a))
    from tests.unit.helpers import offline_settings
    g = gm.GeminiClient.__new__(gm.GeminiClient)
    g.s, g.audit_id, g.usd = offline_settings(), 7, 0.0
    resp = {"candidates": [{"content": {"parts": [{"text": "Top makers: Acme, Rival."}]}, "finishReason": "STOP",
                            "groundingMetadata": {"webSearchQueries": ["go kart makers india"],
                                                  "groundingChunks": [{"web": {"uri": "https://vertexaisearch.cloud.google.com/x",
                                                                               "title": "acme.com"}}]}}],
            "usageMetadata": {"promptTokenCount": 1000, "candidatesTokenCount": 500}}
    a = asyncio.run(g._parse("ai_visibility.answer", resp, grounded=True))
    assert a.text.startswith("Top makers") and a.cited_urls == ["https://acme.com"] and a.searched == 1
    expected = (1000 * 0.30 + 500 * 2.50) / 1e6 + 0.035
    assert abs(g.usd - expected) < 1e-9 and recorded[0][0] == "vertex_gemini"
    blocked = {"candidates": [{"finishReason": "SAFETY"}], "usageMetadata": {}}
    with pytest.raises(Exception, match="declined"):
        asyncio.run(g._parse("t", blocked, grounded=False))


def test_ai_visibility_with_two_engines(monkeypatch):
    from app.modules import m16_ai_visibility as m16

    class FakeGemini:
        async def answer(self, *, task, prompt, grounded=True):
            return Answer("Acme is a leading maker.", [], 1, "gemini-2.5-flash")
    monkeypatch.setattr(m16, "make_gemini", lambda s, a: FakeGemini())
    c = ctx(_site())
    c.inputs.brand = "Acme"
    c.llm = FakeLLM({"ai_visibility.prompts": {"prompts": ["best karts", "kart speed"]}},
                    lambda p: Answer("Karts are fun.", [], 0, "claude-sonnet-5-5"))
    vis = run(AIVisibility(), c)["ai.visibility"]
    assert "by engine: Claude 0%, Gemini 100%" in vis.summary and "Brand mentioned in 50%" in vis.summary
    assert any(s.startswith("Gemini (gemini-2.5-flash") for s in vis.sources)
