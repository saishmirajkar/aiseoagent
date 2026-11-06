"""Unit tests for Phase 2 modules: Performance (8), Schema (9), Internal Linking (4)."""
import json

import pytest

from app.config import RuntimeSettings
from app.modules import m08_performance
from app.modules.base import Status
from app.modules.m04_internal_links import InternalLinking, norm_anchor
from app.modules.m08_performance import Performance, pick_pages
from app.modules.m09_schema import SchemaMarkup, detect_types, generate, social_profiles
from app.modules.schema_rules import canonical_type, is_valid_jsonld, validate
from app.modules.textsim import TfIdf
from app.providers.pagespeed import SOURCE_FIELD, SOURCE_LAB, PsiResult, parse, rate
from app.sitemodel.types import UrlStatus

from tests.unit.helpers import BASE, ctx, img, link, page, run, site, urls

PERF, SCHEMA, LINKS = Performance(), SchemaMarkup(), InternalLinking()


# =========================================================================== PageSpeed parsing
PSI = {
    "loadingExperience": {"metrics": {
        "LARGEST_CONTENTFUL_PAINT_MS": {"percentile": 3100},
        "INTERACTION_TO_NEXT_PAINT": {"percentile": 150},
        "CUMULATIVE_LAYOUT_SHIFT_SCORE": {"percentile": 30}}},
    "lighthouseResult": {
        "lighthouseVersion": "13.6.0",
        "categories": {"performance": {"score": 0.62, "auditRefs": [
            {"id": "render-blocking-insight", "group": "insights"},
            {"id": "unused-css-rules", "group": "diagnostics"},
            {"id": "legacy-dup", "group": "hidden"}]}},
        "audits": {
            "largest-contentful-paint": {"numericValue": 4200.4},
            "cumulative-layout-shift": {"numericValue": 0.012},
            "total-blocking-time": {"numericValue": 350},
            "render-blocking-insight": {"score": 0, "title": "Render-blocking requests",
                                        "metricSavings": {"LCP": 750, "FCP": 750}, "details": {"type": "table"}},
            "unused-css-rules": {"score": 0.5, "title": "Reduce unused CSS",
                                 "details": {"type": "opportunity", "overallSavingsBytes": 51200}},
            "image-delivery-insight": {"score": 1, "title": "fine", "metricSavings": {"LCP": 0}},
            "legacy-dup": {"score": 0, "title": "Superseded audit", "details": {"overallSavingsMs": 900}},
        }}}


def test_parse_field_lab_and_opportunities():
    r = parse("https://ex.com/", "mobile", PSI)
    assert r.score == 62
    assert r.field == {"lcp_ms": 3100.0, "inp_ms": 150.0, "cls": 0.3}  # CLS is reported × 100
    assert r.lab["lcp_ms"] == 4200 and r.lab["tbt_ms"] == 350
    ids = [o["id"] for o in r.opportunities]
    assert ids == ["render-blocking-insight", "unused-css-rules"]  # passing + hidden audits excluded
    assert r.opportunities[1]["savings_kb"] == 50


def test_parse_runtime_error():
    r = parse("u", "mobile", {"lighthouseResult": {"runtimeError": {"code": "NO_FCP", "message": "x"}}})
    assert r.error and "NO_FCP" in r.error


def test_rate_thresholds():
    assert rate("lcp_ms", 2500) == "good" and rate("lcp_ms", 2501) == "needs improvement"
    assert rate("lcp_ms", 4001) == "poor" and rate("cls", 0.1) == "good" and rate("inp_ms", 600) == "poor"


# =========================================================================== Performance module
def _perf_ctx(pages, key=True):
    return ctx(site(pages), integrations={"pagespeed": key})


def _stub(monkeypatch, results: dict):
    async def fake(client, url, strategy, audit_id):
        r = results.get((url, strategy)) or results.get(url)
        if callable(r):
            r = r(url, strategy)
        return r
    monkeypatch.setattr(m08_performance, "_fetch_one", fake)


def test_perf_no_key():
    r = run(PERF, _perf_ctx([page("/")], key=False))
    assert {x.status for x in r.values()} == {Status.NOT_CHECKED}
    assert "PAGESPEED_API_KEY" in r["perf.lcp"].reason


def test_perf_field_data_preferred_and_inp(monkeypatch):
    _stub(monkeypatch, {BASE + "/": lambda u, s: parse(u, s, PSI)})
    r = run(PERF, _perf_ctx([page("/")]))
    lcp = r["perf.lcp"]
    assert lcp.status == Status.WARNING and lcp.sources == [SOURCE_FIELD]  # 3.1 s field (lab 4.2 s ignored)
    assert lcp.items[0].evidence["value"] == "3.1 s"
    assert r["perf.inp"].status == Status.PASS
    assert r["perf.cls"].status == Status.FAIL  # field CLS 0.30 > 0.25
    assert r["perf.tbt"].status == Status.WARNING and r["perf.tbt"].sources == [SOURCE_LAB]
    assert r["perf.score"].status == Status.WARNING
    opp = r["perf.opportunities"]
    assert opp.status == Status.WARNING and opp.items[0].generated_fix["note"]


def test_perf_lab_only_inp_not_checked(monkeypatch):
    lab_only = {**PSI, "loadingExperience": {}}
    _stub(monkeypatch, {BASE + "/": lambda u, s: parse(u, s, lab_only)})
    r = run(PERF, _perf_ctx([page("/")]))
    assert r["perf.lcp"].status == Status.FAIL and r["perf.lcp"].sources == [SOURCE_LAB]  # 4.2 s lab = poor
    assert r["perf.inp"].status == Status.NOT_CHECKED and "CrUX" in r["perf.inp"].reason


def test_perf_passing_checks_keep_measurements(monkeypatch):
    good = {"lighthouseResult": {"categories": {"performance": {"score": 0.98, "auditRefs": []}},
                                 "audits": {"largest-contentful-paint": {"numericValue": 1200},
                                            "cumulative-layout-shift": {"numericValue": 0},
                                            "total-blocking-time": {"numericValue": 10}}}}
    _stub(monkeypatch, {BASE + "/": lambda u, s: parse(u, s, good)})
    r = run(PERF, _perf_ctx([page("/")]))
    assert r["perf.lcp"].status == Status.PASS and len(r["perf.lcp"].items) == 2  # mobile + desktop values shown
    assert r["perf.lcp"].affected == 0


def test_perf_all_errors(monkeypatch):
    _stub(monkeypatch, {BASE + "/": lambda u, s: PsiResult(u, s, error="HTTP 400: Lighthouse could not load")})
    r = run(PERF, _perf_ctx([page("/")]))
    assert r["perf.lcp"].status == Status.NOT_CHECKED and "could not load" in r["perf.lcp"].reason


def test_pick_pages_homepage_plus_templates():
    pages = [page("/", links=[link(f"/blog/p{i}") for i in range(4)] + [link("/products/a"), link("/about")])]
    pages += [page(f"/blog/p{i}") for i in range(4)] + [page("/products/a"), page("/about")]
    c = ctx(site(pages))
    chosen = pick_pages(c, 2)
    assert chosen[0].url == BASE + "/" and len(chosen) == 3
    assert {p.template_key for p in chosen[1:]} == {"/blog/*", "/products/*"} or "/blog/*" in {p.template_key for p in chosen}


# =========================================================================== schema rules
def test_validate_rules():
    ok_product = {"@context": "https://schema.org", "@type": "Product", "name": "Kart",
                  "offers": {"@type": "Offer", "price": "100", "priceCurrency": "INR"}}
    assert all(v.ok for v in validate(ok_product))
    no_offer = {"@context": "https://schema.org", "@type": "Product", "name": "Kart"}
    assert any("offers|review|aggregateRating" in v.missing_required for v in validate(no_offer))
    bad_offer = {"@context": "https://schema.org", "@type": "Product", "name": "K", "offers": {"price": "1"}}
    assert any(v.type == "offers[0].Offer" and v.missing_required for v in validate(bad_offer))
    crumbs = {"@context": "https://schema.org", "@type": "BreadcrumbList",
              "itemListElement": [{"@type": "ListItem", "position": 1, "name": "Home"}]}
    assert any("at least 2" in e for v in validate(crumbs) for e in v.errors)
    assert any("@context" in e for v in validate({"@type": "Organization", "name": "x"}) for e in v.errors)
    assert any("no @type" in e for v in validate({"name": "x"}) for e in v.errors)
    assert canonical_type("Dentist") == "LocalBusiness" and canonical_type("BlogPosting") == "Article"
    assert canonical_type("AutoPartsStore") == "LocalBusiness" and canonical_type("Thing") is None
    # microdata items have no @context: not an error
    assert all(not v.errors for v in validate({"@type": "Organization", "name": "x"}, jsonld=False))


# =========================================================================== schema module
def _sd(*items, errors=None):
    types = sorted({t for i in items for t in ([i["@type"]] if isinstance(i.get("@type"), str) else i.get("@type", []))})
    return {"jsonld": list(items), "microdata": [], "rdfa": [], "errors": errors or [], "types": types}


ORG = {"@context": "https://schema.org", "@type": "Organization", "name": "Acme", "url": BASE + "/",
       "logo": BASE + "/logo.png", "sameAs": ["https://x.com/acme"]}
WEBSITE = {"@context": "https://schema.org", "@type": "WebSite", "name": "Acme", "url": BASE + "/"}


def test_schema_healthy_homepage():
    s = site([page("/", structured_data=_sd(ORG, WEBSITE))])
    r = run(SCHEMA, ctx(s))
    for cid in ("schema.syntax", "schema.required", "schema.recommended", "schema.conflicts", "schema.missing_types"):
        assert r[cid].status == Status.PASS, (cid, r[cid].items)


def test_schema_errors_required_conflicts():
    other_org = {**ORG, "name": "Acme Ltd"}
    bad_product = {"@context": "https://schema.org", "@type": "Product", "name": "Kart"}
    s = site([page("/", structured_data=_sd(ORG, other_org, WEBSITE, errors=["JSON-LD syntax error: x"]),
                   links=[link("/products/kart")]),
              page("/products/kart", structured_data=_sd(bad_product))])
    r = run(SCHEMA, ctx(s))
    assert urls(r["schema.syntax"]) == {BASE + "/"}
    assert urls(r["schema.required"]) == {BASE + "/products/kart"}
    conf = r["schema.conflicts"].items[0].evidence["conflicts"][0]
    assert conf["type"] == "Organization" and conf["problem"] == "different names"


def test_schema_missing_types_and_generated_jsonld():
    s = site([page("/", title="Acme Widgets | Durable widgets", links=[link("/blog/how-to-choose"), link("/products/red-widget")],
                   meta_description="Home description that is unique to the homepage."),
              page("/blog/how-to-choose", word_count=600, dates={"datePublished": "2025-01-02"},
                   authors=["Jane Doe"], headings=[(1, "How to choose a widget")], meta_description="Blog desc unique."),
              page("/products/red-widget", headings=[(1, "Red Widget")], text="Red Widget ₹1,499 Add to cart",
                   images=[img("/i/red.jpg")], meta_description="Product desc unique."),
              ], sitemap={})
    r = run(SCHEMA, ctx(s))
    miss = r["schema.missing_types"]
    by = {(i.url, i.evidence["missing_type"]): i.generated_fix for i in miss.items}
    assert set(by) == {(BASE + "/", "Organization"), (BASE + "/", "WebSite"), (BASE + "/blog/how-to-choose", "Article"),
                       (BASE + "/blog/how-to-choose", "BreadcrumbList"), (BASE + "/products/red-widget", "Product"),
                       (BASE + "/products/red-widget", "BreadcrumbList")}
    for fix in by.values():
        assert fix["validated"], fix
        obj = json.loads(fix["code"].split("\n", 1)[1].rsplit("\n", 1)[0])
        assert obj["@context"] == "https://schema.org"
    product = json.loads(by[(BASE + "/products/red-widget", "Product")]["code"].split("\n", 1)[1].rsplit("\n", 1)[0])
    assert product["offers"]["price"] == "1499" and product["offers"]["priceCurrency"] == "INR"
    assert product["name"] == "Red Widget" and product["brand"]["name"] == "Acme Widgets"
    article = json.loads(by[(BASE + "/blog/how-to-choose", "Article")]["code"].split("\n", 1)[1].rsplit("\n", 1)[0])
    assert article["@type"] == "BlogPosting" and article["datePublished"] == "2025-01-02"
    assert article["author"]["name"] == "Jane Doe"


def test_generated_product_without_price_lists_needs_input():
    pages = [page("/", links=[link("/products/kart")]),
             page("/products/kart", headings=[(1, "Genesis Kart")], text="A great kart", meta_description="same"),
             page("/products/other", meta_description="same")]
    s = site(pages)
    fix = generate("Product", s.pages[BASE + "/products/kart"], s, ctx(s))
    assert {"offers.price", "offers.priceCurrency", "description"} <= set(fix["needs_input"])
    assert fix["validated"]  # missing required fields are declared, not invented
    assert '"description"' not in fix["code"]  # shared meta description is not reused


def test_social_profiles_sitewide_vs_body_link():
    fb, client = "https://www.facebook.com/kartsnkitcars/", "https://www.pinterest.com/someclient/"
    pages = [page("/", links=[link(fb), link(client), link("/a"), link("/b")]),
             page("/a", links=[link(fb)]), page("/b", links=[link(fb)])]
    s = site(pages)
    sure, unsure = social_profiles(s, "KnK Karts")
    assert sure == [fb] and unsure == [client]
    one = site([page("/", links=[link("https://www.instagram.com/acme_widgets/"), link(client)])])
    sure1, unsure1 = social_profiles(one, "Acme")
    assert sure1 == ["https://www.instagram.com/acme_widgets/"] and unsure1 == [client]


def test_video_and_local_business_detection():
    p = page("/videos/demo", embeds=["https://www.youtube.com/embed/dQw4w9WgXcQ"])
    s = site([page("/", links=[link("/videos/demo")]), p])
    assert ("VideoObject", "1 video embed(s)") in detect_types(s.pages[p.url], s, local=False)
    fix = generate("VideoObject", s.pages[p.url], s, ctx(s))
    assert "i.ytimg.com/vi/dQw4w9WgXcQ" in fix["code"] and "uploadDate" in fix["needs_input"]
    assert detect_types(s.pages[BASE + "/"], s, local=True)[0][0] == "LocalBusiness"
    c = ctx(s)
    c.inputs.address = "1 Main St, Pune"
    lb = generate("LocalBusiness", s.pages[BASE + "/"], s, c)
    assert '"PostalAddress"' in lb["code"] and lb["validated"]
    assert is_valid_jsonld({"@context": "https://schema.org", "@type": "LocalBusiness", "name": "x"})[0] is False


# =========================================================================== internal linking
def test_norm_anchor():
    assert norm_anchor("  Read more → ") == "read more" and norm_anchor("Click here!") == "click here"


def _linking_site(**facts):
    pages = [page("/", links=[link("/a", anchor="About us"), link("/b", anchor="Read more"),
                              link("/c", anchor="", nofollow=True), link("/a", anchor="", image_link=True)]),
             page("/a", links=[link("/b", anchor="Read more")]),
             page("/b", links=[link("/a", anchor="About")]),
             page("/c"),
             page("/orphan", depth=None)]
    return site(pages, sitemap={BASE + u: None for u in ("/", "/a", "/b", "/c", "/orphan")}, **facts)


def test_internal_links_checks():
    s = _linking_site(crawl={"limit_reached": False, "link_crawl_complete": True})
    r = run(LINKS, ctx(s))
    orph = r["links.orphans"]
    assert orph.status == Status.FAIL and urls(orph) == {BASE + "/orphan"} and "<a href>" in orph.summary
    assert urls(r["links.few_inlinks"]) == {BASE + "/a", BASE + "/b", BASE + "/c"}
    gen = r["links.generic_anchors"]
    assert gen.affected == 2 and len(gen.items) == 1 and gen.items[0].evidence["pages"] == 2  # same pattern grouped
    assert r["links.empty_anchors"].affected == 1  # image link excluded (covered by image alt check)
    assert r["links.nofollow_internal"].affected == 1


def test_orphans_uncertain_when_crawl_cut_short():
    s = _linking_site(crawl={"limit_reached": True, "link_crawl_complete": False})
    orph = run(LINKS, ctx(s))["links.orphans"]
    assert orph.status == Status.WARNING and "raise the page limit" in orph.summary


def test_deep_pages():
    chain = [page("/", links=[link("/d1")])] + [page(f"/d{i}", links=[link(f"/d{i + 1}")]) for i in range(1, 5)] + [page("/d5")]
    r = run(LINKS, ctx(site(chain)))
    assert urls(r["links.deep_pages"]) == {BASE + "/d4", BASE + "/d5"}


def test_link_suggestions_topic_and_cluster():
    karts = "kart racing track chassis engine " * 10
    garden = "rose garden soil compost watering pruning flowers " * 30
    noise = "privacy cookie newsletter subscribe footer menu " * 5
    pages = [page("/", text=noise + "welcome", links=[link("/blog"), link("/blog/garden-tips")]),
             page("/blog", text=noise + "blog index", headings=[(1, "Blog")], links=[link("/blog/garden-tips")]),
             page("/blog/electric-karts", text=noise + karts + "electric battery charging range " * 20,
                  headings=[(1, "Electric Karts Guide")]),
             page("/blog/kart-motors", text=noise + karts + "motor torque battery electric gearing " * 20,
                  headings=[(1, "Kart Motors")], links=[link("/blog")]),
             page("/blog/garden-tips", text=noise + garden, headings=[(1, "Garden Tips")], links=[link("/blog")]),
             page("/blog/kart-chassis", text=noise + karts + "frame stiffness welding tubes axle " * 20,
                  headings=[(1, "Kart Chassis")], links=[link("/blog")])]
    s = site(pages, sitemap={p.url: None for p in pages})
    r = run(LINKS, ctx(s))["links.suggestions"]
    assert r.status == Status.WARNING
    contextual = [i for i in r.items if i.evidence["kind"] == "contextual"]
    to_ek = [i for i in contextual if i.evidence["add_link_to"] == BASE + "/blog/electric-karts"]
    assert to_ek and all(i.url != BASE + "/blog/garden-tips" for i in to_ek)  # off-topic page not suggested
    assert all("privacy" not in i.evidence["shared_topics"] for i in contextual)  # boilerplate ignored
    kinds = {i.evidence["kind"] for i in r.items}
    assert "pillar → cluster" in kinds  # /blog should link to its posts
    assert any(i.generated_fix["code"].startswith('<a href="') for i in r.items)


def test_tfidf_boilerplate_removed():
    words = ["apple", "banana", "cherry", "damson", "elder", "fig", "grape", "hazel"]
    t = TfIdf({w: f"menu footer subscribe {w}" for w in words})
    assert "menu" not in t.vec["apple"] and "apple" in t.vec["apple"]


def test_tfidf_section_boilerplate_removed():
    words = ["apple", "banana", "cherry", "damson", "elder", "fig", "grape", "hazel"]
    docs = {f"blog-{w}": f"placeholder landscape sunrise {w}" for w in words}
    docs.update({f"shop-{w}": f"price cart {w}fruit" for w in words[:2]})
    groups = {k: k.split("-")[0] for k in docs}
    t = TfIdf(docs, groups=groups, boilerplate_df=0.9)
    assert "landscape" not in t.vec["blog-apple"] and "apple" in t.vec["blog-apple"]
