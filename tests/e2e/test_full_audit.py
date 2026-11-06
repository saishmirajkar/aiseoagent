"""End-to-end: run a full audit (crawl -> Site Model -> modules -> score -> reports) against
the local fixture site, through the real orchestrator and Postgres."""
from __future__ import annotations

import asyncio
import json

import pytest
from sqlalchemy import select

from tests.conftest import free_port
from tests.fixtures.site_server import ServerThread, build_app, build_external_app

pytestmark = pytest.mark.e2e


@pytest.fixture(scope="module")
def audit_result(database_url):
    from app.config import RuntimeSettings
    from app.db.models import Audit, Site
    from app.db.session import session_scope
    from app.orchestrator import run_audit

    site_port, ext_port = free_port(), free_port()
    base = f"http://127.0.0.1:{site_port}"
    with ServerThread(build_external_app(), "127.0.0.1", ext_port), \
            ServerThread(build_app(f"http://localhost:{ext_port}"), "127.0.0.1", site_port):
        with session_scope() as db:
            site = Site(domain=f"127.0.0.1-{site_port}")
            db.add(site)
            db.flush()
            a = Audit(site_id=site.id, input={"url": base + "/"}, settings_snapshot=RuntimeSettings().model_dump())
            db.add(a)
            db.flush()
            audit_id = a.id
        status = asyncio.run(run_audit(audit_id))
    from app.reports.build import build_report_data
    with session_scope() as db:
        data = build_report_data(db, audit_id)
        audit = db.get(Audit, audit_id)
        db.expunge(audit)
    return {"status": status, "base": base, "data": data, "audit": audit}


def _f(data, check_id):
    for lst in data["findings_by_category"].values():
        for f in lst:
            if f["check_id"] == check_id:
                return f
    raise AssertionError(f"{check_id} not in report")


def _urls(f):
    return {i["url"] for i in f["items"]}


def test_audit_completes(audit_result):
    assert audit_result["status"] == "completed"
    a = audit_result["audit"]
    assert a.report_keys.keys() >= {"html", "json", "csv"}
    assert a.scores["overall"] is not None
    assert 0 < a.coverage < 100  # planned modules are NOT_CHECKED, so coverage is partial


def test_all_17_modules_reported(audit_result):
    numbers = {f["module_number"] for lst in audit_result["data"]["findings_by_category"].values() for f in lst}
    assert numbers == set(range(1, 18))


def test_technical_findings(audit_result):
    d, b = audit_result["data"], audit_result["base"]
    assert f"{b}/missing" in _urls(_f(d, "tech.status_4xx"))
    assert _f(d, "tech.broken_internal_links")["status"] == "FAIL"
    assert f"{b}/" in _urls(_f(d, "tech.broken_internal_links"))
    assert any("dead" in json.dumps(i) for i in _f(d, "tech.broken_external_links")["items"])
    chains = _f(d, "tech.redirect_chains")
    assert f"{b}/old-page" in _urls(chains)
    assert chains["items"][0]["generated_fix"]["type"] == "redirect_rule"
    assert f"{b}/loop-a" in _urls(_f(d, "tech.redirect_loops"))
    assert f"{b}/team" in _urls(_f(d, "tech.canonical_broken"))
    assert f"{b}/about" in _urls(_f(d, "tech.viewport"))
    assert f"{b}/blog/post-1" in _urls(_f(d, "tech.lang"))
    assert _f(d, "tech.https")["status"] == "FAIL"  # fixture is plain HTTP
    assert f"{b}/" in _urls(_f(d, "tech.hreflang"))  # de alternate is 404 / no return link
    js = _f(d, "tech.js_dependent")
    assert f"{b}/js-page" in _urls(js)


def test_js_only_link_was_discovered(audit_result):
    urls = {p["url"] for p in audit_result["data"]["pages"]}
    assert f"{audit_result['base']}/js-only-target" in urls


def test_indexing_findings(audit_result):
    d, b = audit_result["data"], audit_result["base"]
    assert _f(d, "crawl.robots_txt")["status"] == "PASS"
    assert _f(d, "crawl.sitemap_present")["status"] == "PASS"
    assert f"{b}/gone" in _urls(_f(d, "crawl.sitemap_bad_urls"))
    assert f"{b}/noindex-page" in _urls(_f(d, "crawl.sitemap_non_indexable"))
    assert f"{b}/js-only-target" in _urls(_f(d, "crawl.sitemap_missing_pages"))
    assert _f(d, "crawl.robots_blocks_resources")["status"] == "WARNING"
    assert _f(d, "crawl.gsc_index_coverage")["status"] == "NOT_CHECKED"
    assert "Search Console" in _f(d, "crawl.gsc_index_coverage")["reason"]
    assert d["site"]["ai_bots"]["GPTBot"]["root_allowed"] is False


def test_meta_findings(audit_result):
    d, b = audit_result["data"], audit_result["base"]
    dup = _urls(_f(d, "meta.title_duplicate"))
    assert {f"{b}/about", f"{b}/orphan"} <= dup
    assert f"{b}/team" not in dup  # canonicalised elsewhere: not a duplicate-title problem
    assert f"{b}/blog/post-1" in _urls(_f(d, "meta.title_too_long"))
    assert f"{b}/about" in _urls(_f(d, "meta.description_missing"))
    assert f"{b}/blog/post-1" in _urls(_f(d, "meta.description_length"))
    assert _f(d, "meta.keyword_in_title")["status"] == "NOT_CHECKED"


def test_image_findings(audit_result):
    d = audit_result["data"]
    heavy = _f(d, "img.file_size")
    assert heavy["status"] == "FAIL" and "IMG_1234.jpg" in json.dumps(heavy["items"])
    assert "IMG_1234.jpg" in json.dumps(_f(d, "img.filename")["items"])
    assert "IMG_1234.jpg" in json.dumps(_f(d, "img.alt_missing")["items"])
    assert "IMG_1234.jpg" in json.dumps(_f(d, "img.oversized_dimensions")["items"])
    assert "hero.png" in json.dumps(_f(d, "img.lazy_above_fold")["items"])
    assert _f(d, "img.alt_empty")["status"] == "WARNING"  # linked logo with alt=""
    assert _f(d, "img.alt_generation")["status"] == "NOT_CHECKED"


def test_missing_summary_and_top10(audit_result):
    d = audit_result["data"]
    assert any("Meta descriptions missing" in m for m in d["missing"])
    assert 0 < len(d["top_fixes"]) <= 10
    pr = [f["priority"] for f in d["top_fixes"]]
    assert pr == sorted(pr, reverse=True)


def test_reports_render(audit_result):
    from app.reports.build import render_csv, render_html, render_json
    d = audit_result["data"]
    html = render_html(d)
    assert "Missing on this site" in html and "Top 10 fixes" in html
    assert json.loads(render_json(d))["audit"]["id"] == d["audit"]["id"]
    csv_text = render_csv(d)
    assert csv_text.splitlines()[0].startswith("audit_id,category")
    assert "tech.redirect_loops" in csv_text


def test_persisted_site_model(audit_result, db):
    from app.db.models import Image, Link, Page, PageText
    aid = audit_result["audit"].id
    pages = db.execute(select(Page).where(Page.audit_id == aid)).scalars().all()
    assert len(pages) >= 10
    assert db.execute(select(Link).where(Link.audit_id == aid)).first() is not None
    assert db.execute(select(Image).where(Image.audit_id == aid)).first() is not None
    assert db.execute(select(PageText).join(Page, Page.id == PageText.page_id).where(Page.audit_id == aid)).first()


def test_phase2_modules(audit_result):
    d, b = audit_result["data"], audit_result["base"]
    orph = _f(d, "links.orphans")
    assert orph["status"] == "FAIL" and f"{b}/orphan" in _urls(orph)  # link crawl was complete
    assert _f(d, "links.generic_anchors")["affected"] >= 1  # "Read more" on the blog post
    missing = {(i["url"], i["evidence"]["missing_type"]) for i in _f(d, "schema.missing_types")["items"]}
    assert (f"{b}/", "Organization") in missing and (f"{b}/", "WebSite") in missing
    assert all(i["generated_fix"]["validated"] for i in _f(d, "schema.missing_types")["items"])
    perf = _f(d, "perf.lcp")
    assert perf["status"] == "NOT_CHECKED" and "PAGESPEED_API_KEY" in perf["reason"]
