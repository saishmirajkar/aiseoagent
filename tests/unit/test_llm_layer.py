"""LLM gateway (budget, cache, cost, refusals) and Phase 3 modules with a fake LLM."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from app.config import LLMSettings, RuntimeSettings
from app.llm.client import BudgetExceeded, LLMClient, LLMError, LLMRefused, obj, STR
from app.llm.pricing import usage_cost, worst_case_cost
from app.llm.summaries import analyse, select_pages
from app.modules.base import Status
from app.modules.m01_keywords import KeywordResearch, similar
from app.modules.m02_content import ContentOptimization, faq_jsonld, flesch
from app.modules.m03_meta_tags import MetaTags
from app.modules.m04_internal_links import InternalLinking
from app.modules.m05_images import ImageSEO
from app.modules.m17_geo import GeoContent

from tests.unit.helpers import BASE, asset, ctx, img, link, page, run, site, urls


# =========================================================================== fakes
class FakeLLM:
    """Stands in for LLMClient in module tests: responses keyed by task name (callable or dict)."""

    def __init__(self, responses: dict, fail: dict | None = None):
        self.responses = responses
        self.fail = fail or {}
        self.calls: list[dict] = []

    async def json(self, *, task, role, system, user, schema, max_tokens=8000, use_cache=True):
        self.calls.append({"task": task, "role": role, "user": user, "system": system})
        if task in self.fail:
            raise self.fail[task]
        r = self.responses.get(task)
        return r(user) if callable(r) else (r or {})


def _urls_in(user) -> list[str]:
    text = user if isinstance(user, str) else json.dumps(user)
    import re
    return re.findall(r"URL: (\S+)", text)


def fake_resp(text: str, *, stop="end_turn", model="claude-sonnet-5-5", inp=1000, out=200):
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], stop_reason=stop, model=model,
                           stop_details=SimpleNamespace(category="cyber") if stop == "refusal" else None,
                           usage=SimpleNamespace(input_tokens=inp, output_tokens=out, cache_creation_input_tokens=0,
                                                 cache_read_input_tokens=0))


class FakeAnthropic:
    def __init__(self, resp):
        self.resp = resp
        self.kwargs: list[dict] = []
        outer = self

        class _M:
            async def create(self, **kw):
                outer.kwargs.append(kw)
                return outer.resp
        self.messages = _M()
        self.beta = SimpleNamespace(messages=_M())


# =========================================================================== pricing
def test_pricing_math():
    assert usage_cost("claude-sonnet-5-5", 1_000_000, 100_000) == pytest.approx(2.0 + 1.0)
    assert usage_cost("claude-haiku-5-5", 1_000_000, 0, cache_read=1_000_000) == pytest.approx(0.10 + 0.01)
    assert usage_cost("claude-unknown-9", 1_000_000, 0) == pytest.approx(5.0)  # conservative fallback price
    assert worst_case_cost("claude-sonnet-5-5", 10_000, 6000) == pytest.approx(10_000 * 2.5 / 1e6 + 6000 * 10 / 1e6)


# =========================================================================== gateway (needs Postgres for ledger/cache)
SCHEMA = obj({"answer": STR})


@pytest.mark.db
def test_gateway_cost_cache_and_fallbacks(database_url):
    fake = FakeAnthropic(fake_resp('{"answer": "ok"}'))
    llm = LLMClient(LLMSettings(), audit_id=None, per_audit_usd=1.0, per_month_usd=100.0, client=fake)
    r = asyncio.run(llm.json(task="t", role="reasoning", system="sys-unique-1", user="hello", schema=SCHEMA, max_tokens=500))
    assert r == {"answer": "ok"}
    kw = fake.kwargs[-1]
    assert kw["model"] == "claude-sonnet-5-5" and kw["fallbacks"] == "default"
    assert kw["betas"] == ["server-side-fallback-2026-07-01"]
    assert kw["output_config"]["format"]["schema"] == SCHEMA and kw["output_config"]["effort"] == "low"
    assert "temperature" not in kw and "thinking" not in kw  # rejected / not needed on Sonnet 5.5
    assert kw["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert llm.stats.usd == pytest.approx(usage_cost("claude-sonnet-5-5", 1000, 200))
    # identical request -> cache, no new API call
    asyncio.run(llm.json(task="t", role="reasoning", system="sys-unique-1", user="hello", schema=SCHEMA, max_tokens=500))
    assert len(fake.kwargs) == 1 and llm.stats.cached == 1
    # bulk role: Haiku, no fallbacks (none exist for Haiku 5.5)
    asyncio.run(llm.json(task="t", role="bulk", system="sys-unique-2", user="x", schema=SCHEMA, max_tokens=300))
    assert fake.kwargs[-1]["model"] == "claude-haiku-5-5" and "fallbacks" not in fake.kwargs[-1]


@pytest.mark.db
def test_gateway_budget_refusal_truncation(database_url):
    llm = LLMClient(LLMSettings(cache_days=0), audit_id=None, per_audit_usd=0.01, per_month_usd=100.0,
                    client=FakeAnthropic(fake_resp('{"answer": "ok"}')))
    with pytest.raises(BudgetExceeded):  # worst case of 8,000 output tokens on Sonnet = $0.08 > $0.01
        asyncio.run(llm.json(task="t", role="reasoning", system="s", user="u", schema=SCHEMA, max_tokens=8000))
    assert isinstance(BudgetExceeded("x"), LLMError)  # modules catch LLMError and report NOT_CHECKED
    ref = LLMClient(LLMSettings(cache_days=0), audit_id=None, per_audit_usd=5, per_month_usd=100,
                    client=FakeAnthropic(fake_resp("", stop="refusal")))
    with pytest.raises(LLMRefused):
        asyncio.run(ref.json(task="t", role="reasoning", system="s", user="u", schema=SCHEMA, max_tokens=100))
    assert ref.stats.usd > 0  # a refusal is still recorded in the ledger
    trunc = LLMClient(LLMSettings(cache_days=0), audit_id=None, per_audit_usd=5, per_month_usd=100,
                      client=FakeAnthropic(fake_resp('{"ans', stop="max_tokens")))
    with pytest.raises(LLMError, match="truncated"):
        asyncio.run(trunc.json(task="t", role="reasoning", system="s", user="u", schema=SCHEMA, max_tokens=100))


@pytest.mark.db
def test_gateway_monthly_cap(database_url, db):
    from app.db.models import UsageLedger
    db.add(UsageLedger(provider="anthropic", kind="seed", units=1, cost_usd=29.99))
    db.commit()
    llm = LLMClient(LLMSettings(cache_days=0), audit_id=None, per_audit_usd=5, per_month_usd=30.0,
                    client=FakeAnthropic(fake_resp('{"answer": "ok"}')))
    with pytest.raises(BudgetExceeded, match="Monthly"):
        asyncio.run(llm.json(task="t", role="reasoning", system="s", user="u", schema=SCHEMA, max_tokens=4000))
    db.query(UsageLedger).filter(UsageLedger.kind == "seed").delete()
    db.commit()


# =========================================================================== summaries
PLACEHOLDER = ("As a professional photographer, there is nothing quite like capturing the perfect landscape shot. "
               "Whether you are trekking through a dense forest or standing at the edge of a cliff, preparation "
               "matters. Plan your shoot around the golden hour for the best light on the scenery. Use a sturdy "
               "tripod and a wide angle lens to capture the full depth of the view in front of you. ")


def _blog_site():
    posts = []
    for i in range(6):
        text = f"Post {i} talks about kart model {i} and its engine. " + PLACEHOLDER
        posts.append(page(f"/blog/post-{i}", text=text, word_count=len(text.split()), template_key="/blog/*"))
    home = page("/", links=[link(p.url) for p in posts], text="Welcome to Acme karts. We build karts.")
    return site([home] + posts)


def test_boilerplate_analysis_finds_placeholder_block():
    s = _blog_site()
    a = analyse(s)
    assert a.blocks and len(a.blocks[0]["pages"]) == 6 and a.blocks[0]["words"] >= 25
    main = a.main_text[BASE + "/blog/post-3"]
    assert "kart model 3" in main and "photographer" not in main
    assert a.repeated_share[BASE + "/blog/post-3"] > 0.8


def test_select_pages_order():
    s = site([page("/", links=[link("/a"), link("/b")]), page("/a", links=[link("/b")]), page("/b"),
              page("/cart", meta_robots="")])
    chosen = [p.url for p in select_pages(s, 3)]
    assert chosen[0] == BASE + "/" and chosen[1] == BASE + "/b"  # /b has most inlinks
    assert BASE + "/cart" not in chosen  # utility pages excluded


# =========================================================================== module 1
def _kw_response(mapping: dict):
    def resp(user):
        return {"pages": [dict(url=u, **mapping.get(u, {"primary_keyword": "", "secondary_keywords": [],
                                                          "long_tail": [], "intent": "informational",
                                                          "cluster": "misc", "confidence": "low"}))
                          for u in _urls_in(user)]}
    return resp


def kwrow(kw, intent="commercial", conf="high"):
    return {"primary_keyword": kw, "secondary_keywords": [kw + " price"], "long_tail": [f"best {kw} for beginners"],
            "intent": intent, "cluster": "karts", "confidence": conf}


def test_keywords_module():
    s = site([page("/", links=[link("/karts"), link("/racing-karts"), link("/about")], title="Acme"),
              page("/karts", title="Go Karts for Sale"), page("/racing-karts", title="Racing go karts"),
              page("/about", title="About")])
    llm = FakeLLM({"keywords.map": _kw_response({BASE + "/": kwrow("acme karts", "navigational"),
                                                  BASE + "/karts": kwrow("go karts for sale"),
                                                  BASE + "/racing-karts": kwrow("go kart for sale")})})
    c = ctx(s)
    c.llm = llm
    c.inputs.keywords = ["go karts for sale", "electric kart"]
    r = run(KeywordResearch(), c)
    assert urls(r["kw.mapping"]) == {BASE + "/about"}  # no keyword / low confidence
    assert urls(r["kw.cannibalization"]) == {BASE + "/karts", BASE + "/racing-karts"}  # plural-insensitive match
    assert r["kw.owner_keywords"].status == Status.WARNING
    assert [i.evidence["keyword"] for i in r["kw.owner_keywords"].items] == ["electric kart"]
    assert r["kw.competitor_gaps"].status == Status.NOT_APPLICABLE
    assert r["kw.search_volume"].status == Status.NOT_CHECKED and "Phase 4" in r["kw.search_volume"].reason
    assert c.shared["keyword_map"][BASE + "/karts"]["primary_keyword"] == "go karts for sale"
    assert all(call["role"] == "reasoning" for call in llm.calls)


def test_keywords_without_llm_or_on_failure():
    s = site([page("/")])
    r = run(KeywordResearch(), ctx(s))
    assert r["kw.mapping"].status == Status.NOT_CHECKED and "ANTHROPIC_API_KEY" in r["kw.mapping"].reason
    c = ctx(s)
    c.llm = FakeLLM({}, fail={"keywords.map": BudgetExceeded("LLM budget for this audit ($2.00) would be exceeded")})
    r2 = run(KeywordResearch(), c)
    assert r2["kw.mapping"].status == Status.NOT_CHECKED and "budget" in r2["kw.mapping"].reason


def test_similar_keywords():
    assert similar("go karts for sale", "go kart sale") and not similar("go kart", "kart tyres")


# =========================================================================== module 2
def test_content_deterministic_checks():
    s = _blog_site()
    s.pages[BASE + "/blog/post-1"].headings = []
    s.pages[BASE + "/blog/post-2"].headings = [(1, "A"), (1, "B"), (2, "C"), (4, "D")]
    r = run(ContentOptimization(), ctx(s))
    rb = r["content.repeated_blocks"]
    assert rb.status == Status.FAIL and rb.affected == 6 and "photographer" in rb.items[0].evidence["sample"]
    assert BASE + "/blog/post-0" in urls(r["content.thin"])  # little unique text once the block is removed
    h1 = r["content.h1"]
    assert h1.status == Status.FAIL and {BASE + "/blog/post-1", BASE + "/blog/post-2"} <= urls(h1)
    assert urls(r["content.heading_order"]) == {BASE + "/blog/post-2"}
    assert r["content.gaps"].status == Status.NOT_CHECKED


def test_flesch_and_faq_jsonld():
    easy = "The cat sat on the mat. It was a good day. We had fun in the sun. " * 15
    hard = ("Notwithstanding considerable organisational heterogeneity, interdepartmental communication "
            "methodologies necessitate comprehensive institutionalisation. ") * 15
    assert flesch(easy) > 80 and flesch(hard) < 30
    assert flesch("too short") is None
    fx = faq_jsonld([{"question": "Q1?", "answer": "A1"}, {"question": "Q2?", "answer": "A2"},
                     {"question": "Q3?", "answer": ""}])
    assert fx["validated"] and '"FAQPage"' in fx["code"] and "Q3" not in fx["code"]
    assert faq_jsonld([{"question": "Q?", "answer": ""}]) is None


def test_content_gaps_with_llm():
    s = site([page("/", links=[link("/karts")], text="Acme builds karts. " * 40),
              page("/karts", text="Our karts are fast. " * 60, headings=[(1, "Karts")])])

    def resp(user):
        return {"pages": [{"url": u, "covers_intent": u.endswith("/"), "missing_subtopics": ["Engine options", "Safety"],
                           "missing_entities": ["CIK-FIA"], "unanswered_questions": ["How fast is it?", "Price?"],
                           "additions": [{"heading": "Engine options", "what_to_write": "List engines"}],
                           "faqs": [{"question": "How fast?", "answer": "Up to 80 km/h"},
                                    {"question": "Warranty?", "answer": "One year"}]} for u in _urls_in(user)]}
    c = ctx(s)
    c.llm = FakeLLM({"content.gaps": resp})
    r = run(ContentOptimization(), c)["content.gaps"]
    assert r.status == Status.WARNING and urls(r) == {BASE + "/karts"}
    fix = r.items[0].generated_fix
    assert fix["faq_jsonld_validated"] and "Engine options" in fix["code"]


# =========================================================================== module 3
def test_meta_keyword_in_title_and_rewrites():
    s = site([page("/", links=[link("/karts")], title="Acme | Home"),
              page("/karts", title="Products", meta_description=None)])
    kmap = {BASE + "/": kwrow("acme karts", "navigational"), BASE + "/karts": kwrow("racing go karts")}
    good_title = "Racing Go Karts for Sale – Acme Karts India"
    resp = lambda user: {"pages": [{"url": u, "intent_aligned": not u.endswith("karts"), "issue": "Generic title",
                                    "titles": [good_title, "X" * 90], "descriptions": ["d" * 140, "short"]}
                                   for u in _urls_in(user)]}
    c = ctx(s)
    c.shared["keyword_map"] = kmap
    c.llm = FakeLLM({"meta.rewrites": resp})
    r = run(MetaTags(), c)
    kit = r["meta.keyword_in_title"]
    item = next(i for i in kit.items if i.url == BASE + "/karts")
    assert "racing" in item.evidence["missing_words"]
    fix = next(i for i in r["meta.description_missing"].items if i.url == BASE + "/karts").generated_fix
    assert fix["titles"][0]["text"] == good_title and fix["titles"][0]["fits"] is True
    assert fix["titles"][1]["fits"] is False and fix["descriptions"][1]["fits"] is False  # lengths checked in code
    ia = r["meta.intent_alignment"]
    assert ia.status == Status.WARNING and urls(ia) == {BASE + "/karts"}


# =========================================================================== module 4 review
def test_link_suggestions_llm_review():
    karts = "kart racing track chassis engine " * 10
    pages = [page("/", text="home", links=[link("/blog")]),
             page("/blog", text="blog index", headings=[(1, "Blog")]),
             page("/blog/a", text=karts + "electric battery charging range " * 20, headings=[(1, "Electric Karts")]),
             page("/blog/b", text=karts + "motor torque battery electric gearing " * 20, headings=[(1, "Kart Motors")],
                  links=[link("/blog")]),
             page("/blog/c", text=karts + "frame stiffness welding tubes axle " * 20, headings=[(1, "Kart Chassis")],
                  links=[link("/blog")])]
    s = site(pages, sitemap={p.url: None for p in pages})

    def resp(user):
        import re
        ids = [int(x) for x in re.findall(r"^(\d+)\. FROM", user.split("<data>\n", 1)[1], re.M)]
        return {"links": [{"id": i, "keep": i % 2 == 0, "anchor": "electric kart guide", "reason": "related"} for i in ids]}
    c = ctx(s)
    c.llm = FakeLLM({"links.review": resp})
    r = run(InternalLinking(), c)["links.suggestions"]
    assert "reviewed by AI" in r.summary
    assert all(i.evidence["anchor"] == "electric kart guide" for i in r.items)
    assert all(i.generated_fix["code"].endswith(">electric kart guide</a>") for i in r.items)


# =========================================================================== module 5 alt text
def test_alt_text_generation():
    imgs = [img("/i/kart.jpg", alt=None), img("/i/logo.svg", alt=None), img("/i/bad.png", alt=None),
            img("/i/ok.png", alt="Fine")]
    assets = {BASE + "/i/kart.jpg": asset("/i/kart.jpg", format="jpeg", bytes=50_000, intrinsic_w=800, intrinsic_h=600),
              BASE + "/i/logo.svg": asset("/i/logo.svg", format="svg", bytes=2_000),
              BASE + "/i/bad.png": asset("/i/bad.png", format="png", bytes=10_000, intrinsic_w=300, intrinsic_h=300),
              BASE + "/i/ok.png": asset("/i/ok.png", format="png", bytes=10_000, intrinsic_w=300, intrinsic_h=300)}
    s = site([page("/", images=imgs, title="Acme Karts")], images=assets)

    def resp(user):
        n_images = sum(1 for b in user if b.get("type") == "image")
        if any(b.get("type") == "image" and b["source"]["url"].endswith("bad.png") for b in user):
            if n_images > 1:
                raise LLMError("Anthropic rejected the request: could not fetch image")
            raise LLMError("could not fetch image")
        return {"images": [{"index": 1, "alt": "Red racing kart on a track", "decorative": False}]}

    class Flaky(FakeLLM):
        async def json(self, **kw):
            self.calls.append(kw)
            return resp(kw["user"])
    c = ctx(s)
    c.llm = Flaky({})
    r = run(ImageSEO(), c)
    gen = r["img.alt_generation"]
    assert gen.status == Status.WARNING and len(gen.items) == 1
    assert gen.items[0].evidence["suggested_alt"] == "Red racing kart on a track"
    assert "1 failed" in gen.summary and "cannot read" in gen.summary  # bad.png isolated; svg skipped
    assert all(call["role"] == "bulk" for call in c.llm.calls)
    miss = next(i for i in r["img.alt_missing"].items if i.evidence["src"].endswith("kart.jpg"))
    assert miss.generated_fix["code"] == 'alt="Red racing kart on a track"'


# =========================================================================== module 17
def test_geo_module():
    article = page("/blog/kart-guide", text="Karts are small vehicles. " * 80, og={"type": "article"},
                   headings=[(1, "Kart guide"), (2, "Engines"), (2, "Chassis"), (3, "Tyres")], authors=[],
                   links=[link("https://www.facebook.com/acme")])
    s = site([page("/", links=[link("/blog/kart-guide"), link("/about")], text="Welcome " * 100), article,
              page("/about")])
    c = ctx(s)
    c.shared["keyword_map"] = {article.url: kwrow("kart guide", "informational")}
    c.llm = FakeLLM({"geo.answer_first": lambda user: {"pages": [
        {"url": u, "answer_first": False, "opening_rewrite": "A kart is a small four-wheeled racing vehicle.",
         "quotable_facts": ["Karts weigh about 75 kg."], "needs_sources": []} for u in _urls_in(user)]}})
    r = run(GeoContent(), c)
    assert r["geo.answer_first"].status == Status.WARNING
    assert r["geo.answer_first"].items[0].generated_fix["code"].startswith("A kart is")
    assert urls(r["geo.question_headings"]) == {article.url}
    assert article.url in urls(r["geo.faq"])
    c2 = ctx(s)
    c2.shared["keyword_map"] = c.shared["keyword_map"]
    c2.shared["content_gaps"] = {article.url: {"faqs": [{"question": "What is a kart?", "answer": "A small racer."},
                                                        {"question": "How fast?", "answer": "80 km/h."}]}}
    faq_item = next(i for i in run(GeoContent(), c2)["geo.faq"].items if i.url == article.url)
    assert "What is a kart?" in faq_item.generated_fix["code"] and "FAQPage" in faq_item.generated_fix["faq_jsonld"]
    assert urls(r["geo.sources_and_stats"]) == {article.url}  # social links are not sources
    auth = r["geo.authorship"].items[0].evidence["missing"]
    assert "author byline" in auth and "last-updated date" in auth
    ee = r["geo.eeat_site"]
    assert ee.status == Status.WARNING and "contact page" in ee.items[0].evidence["missing"]
    assert "about page" not in ee.items[0].evidence["missing"]
