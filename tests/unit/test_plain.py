from app.modules.registry import all_modules
from app.reports.plain import AGENTS, NOUNS, P, agent_summaries, grade, plain_fields, plain_reason


def test_every_module_and_check_has_plain_wording():
    for m in all_modules():
        assert m.key in AGENTS, m.key
        for c in m.checks:
            assert c.id in P, c.id
            if c.population in NOUNS and c.id in P:
                bad = P[c.id]["bad"]
                assert bad and bad[0].islower() and not bad.endswith("."), c.id  # reads as "We found 3 pages <bad>"


def _f(**kw):
    d = {"module": "meta_tags", "check_id": "meta.title_duplicate", "status": "FAIL", "title": "Duplicate titles",
         "affected": 1, "total": 98, "reason": None, "summary": None, "why": "", "fix": "", "priority": 2}
    d.update(kw)
    return d


def test_plain_fields_sentences():
    one = plain_fields(_f(), "pages")
    assert one["agent"] == "Search Listing Agent" and one["plain_status"] == "Needs fixing"
    assert one["plain_found"] == "We found 1 page sharing the same Google headline with other pages (out of 98 checked)."
    assert plain_fields(_f(affected=30), "pages")["plain_found"].startswith("We found 30 pages ")
    assert plain_fields(_f(status="PASS"), "pages")["plain_found"] == "No problems found."
    nc = plain_fields(_f(status="NOT_CHECKED", reason="Requires Google Search Console access. Setup: ..."), "pages")
    assert "Search Console" in nc["plain_found"] and "Setup" not in nc["plain_found"]


def test_reasons_and_grades():
    assert "later update" in plain_reason("This module is not built yet (planned for build phase 5)")
    assert "AI assistant" in plain_reason("Anthropic API key not configured (add the ANTHROPIC_API_KEY secret)")
    assert plain_reason("something odd") == "We couldn't check this automatically this time."
    assert grade(95)["label"] == "Excellent" and grade(80)["label"].startswith("Good") and grade(None)["label"]


def test_agent_summaries_verdicts():
    fs = [_f(), _f(status="WARNING", check_id="meta.title_too_short"), _f(module="images", check_id="img.broken", status="PASS")]
    ag = agent_summaries([{**f, **plain_fields(f)} for f in fs], ["meta_tags", "images", "schema"])
    assert [a["name"] for a in ag] == ["Search Listing Agent", "Image Agent"]
    assert ag[0]["tone"] == "FAIL" and ag[0]["verdict"] == "1 thing to fix, 1 to improve"
    assert ag[1]["verdict"] == "All good"


def test_coverage_confidence_and_access():
    from app.reports.plain import access_groups, confidence, coverage_sentence
    assert "single page" in coverage_sentence({"pages_fetched": 1}, False, 0, True)
    assert "limit set for this audit" in coverage_sentence(
        {"pages_fetched": 100, "limit_reached": True, "link_crawl_complete": False}, True, 250, False)
    assert coverage_sentence({"pages_fetched": 40, "link_crawl_complete": True}, True, 38, False) == \
        "Our agents found and checked all 40 pages reachable by following links and your page list (sitemap) of 38 addresses."
    assert confidence("content.gaps", "WARNING")["level"] == "Medium"
    assert confidence("tech.status_4xx", "FAIL") == {"level": "High", "basis": "Measured directly on your website."}
    assert confidence("tech.status_4xx", "NOT_CHECKED") is None
    g = access_groups([_f(status="NOT_CHECKED", reason="Requires Google Search Console access", plain_title="A"),
                       _f(status="NOT_CHECKED", reason="This module is not built yet", plain_title="B"),
                       _f(status="NOT_CHECKED", reason="Search Console needed", plain_title="C")])
    assert g[0]["checks"] == ["A", "C"] and "later update" in g[-1]["need"]
