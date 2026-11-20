"""'Fix with AI': plan, locate, edits, writing checks, runner + publish on a local git repo, GitHub client."""
import asyncio
import json
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest

from app.fixer import html_edit as H
from app.fixer.detect import detect
from app.fixer.github import GitHubClient, parse_repo
from app.fixer.llm_edit import apply_edits
from app.fixer.locate import Locator, PageInfo
from app.fixer.plan import build_plan
from app.fixer.repo import LocalRepo
from app.fixer.runner import FixRunner, pr_body, publish
from app.fixer.validate import check_file
from app.fixer.writing import lint

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "fixer_site"
O = "https://acme.example"


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def repo_dir(tmp_path):
    d = tmp_path / "site"
    shutil.copytree(FIXTURE, d)
    for args in (["init", "-q", "-b", "main"], ["add", "-A"],
                 ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init"]):
        subprocess.run(["git", "-C", str(d), *args], check=True, capture_output=True)
    return d


# ------------------------------------------------------------------ findings → plan
def F(check, module, status, items, **kw):
    return {"check_id": check, "module": module, "status": status, "items": items, "title": check,
            "plain_title": kw.get("plain_title", check), "plain_found": "", "plain_do": "do it", "fix": "fix"}


def findings():
    rw = {"type": "rewrite", "titles": [{"text": "Go Kart Manufacturer in Pune | Acme Karts", "fits": True}],
          "descriptions": [{"text": "Rental and racing go-karts built in Pune since 2004.", "fits": True}]}
    return [
        F("meta.title_too_short", "meta_tags", "WARNING", [{"url": O + "/", "evidence": {}, "generated_fix": rw}]),
        F("meta.description_missing", "meta_tags", "FAIL", [{"url": O + "/", "evidence": {}, "generated_fix": rw}]),
        F("img.alt_generation", "images", "WARNING", [{"url": O + "/", "also_on": [O + "/about.html"],
          "evidence": {"src": O + "/img/team.jpg", "suggested_alt": "Acme Karts team in the Pune workshop"},
          "generated_fix": {"type": "alt_text", "decorative": False}}]),
        F("img.missing_dimensions", "images", "WARNING", [{"url": O + "/", "evidence": {"src": O + "/img/team.jpg"},
          "generated_fix": {"type": "html_attrs", "code": 'width="800" height="600"'}}]),
        F("tech.canonical_missing", "technical", "WARNING", [{"url": O + "/", "evidence": {}}]),
        F("tech.lang", "technical", "WARNING", [{"url": O + "/", "evidence": {}}]),
        F("schema.missing_types", "schema", "WARNING", [{"url": O + "/", "evidence": {"missing_type": "Organization"},
          "generated_fix": {"type": "jsonld", "needs_input": [], "code": json.dumps(
              {"@context": "https://schema.org", "@type": "Organization", "name": "Acme Karts", "url": O + "/"})}}]),
        F("crawl.sitemap_present", "indexing", "FAIL", []),
        F("geo.answer_first", "geo_content", "WARNING", [{"url": O + "/", "evidence": {},
          "generated_fix": {"code": "Acme Karts builds go-karts in Pune."}}]),
        F("content.h1_missing", "content", "FAIL", [{"url": O + "/", "evidence": {"problem": "H1 present but empty"}}]),
        F("content.placeholder", "content", "FAIL", [{"url": O + "/", "evidence": {"found_text": "Lorem ipsum dolor"}}]),
        F("tech.soft_404", "technical", "FAIL", [], plain_title="Wrong addresses show a 'page not found' message"),
        F("meta.title_too_short", "meta_tags", "PASS", []),
    ]


PAGES = [{"url": O + "/", "title": "Acme Karts", "meta_description": None, "indexable": True, "status_code": 200,
          "data": {"headings": [], "lang": None}},
         {"url": O + "/about.html", "title": "About", "meta_description": "About us", "indexable": True,
          "status_code": 200, "data": {"headings": [[1, "About Acme Karts"]], "lang": "en"}},
         {"url": O + "/contact", "title": "Contact Acme Karts", "meta_description": None, "indexable": True,
          "status_code": 200, "data": {"headings": [[1, "Contact Acme Karts"]]}}]


def test_plan_kinds_and_questions():
    items = build_plan(findings(), PAGES, {"brand": "Acme Karts"})
    by = {(i.action, i.page_url): i for i in items}
    assert by[("set_title", O + "/")].after == "Go Kart Manufacturer in Pune | Acme Karts"
    assert by[("set_title", O + "/")].kind == "auto" and by[("set_description", O + "/")].kind == "auto"
    assert by[("lang", O + "/")].data["lang"] == "en"  # most common language on the site
    assert by[("write_opening", O + "/")].kind == "review"
    assert by[("set_h1", O + "/")].after == "Go Kart Manufacturer in Pune"  # brand suffix removed
    ph = by[("replace_placeholder", O + "/")]
    assert ph.kind == "input" and ph.questions[0]["required"]
    assert any(i.kind == "manual" and "page not found" in i.label for i in items)
    assert len({i.id for i in items}) == len(items)  # stable unique ids


# ------------------------------------------------------------------ locate & detect
def test_detect_and_locate(repo_dir):
    repo = LocalRepo(repo_dir)
    fw = run(detect(repo))
    assert fw.name == "static-html" and fw.public_dir == ""
    loc = Locator(repo, fw)
    home = run(loc.locate(PageInfo(O + "/", "Acme Karts")))
    assert home[0].path == "index.html" and "path matches the page address" in home[0].reasons
    contact = run(loc.locate(PageInfo(O + "/contact", "Contact Acme Karts", h1="Contact Acme Karts")))
    assert contact[0].path == "src/Contact.jsx"


# ------------------------------------------------------------------ deterministic edits & validation
def test_html_edits_and_validation():
    src = (FIXTURE / "index.html").read_text()
    out = H.set_title(src, "Karts & More")
    assert "<title>Karts &amp; More</title>" in out
    assert H.set_title("<title>{{ page.title }}</title>", "x") is None  # template value: never guessed
    out = H.set_meta_description(out, 'Say "hi"')
    assert 'content="Say &quot;hi&quot;"' in out and check_file("index.html", src, out) == []
    out2, n = H.set_img_attrs(out, O + "/img/team.jpg", alt="Team", width=8, height=6)
    assert n == 1 and '<img src="/img/team.jpg" alt="Team" width="8" height="6">' in out2
    broken = out2.replace("</main>", "")
    assert any("</main>" in p for p in check_file("index.html", out2, broken))
    bad_ld = H.insert_jsonld(out2, '{"@type": }')
    assert any("not valid JSON" in p for p in check_file("index.html", out2, bad_ld))
    assert apply_edits("a b a", [{"search": "a", "replace": "x"}]).text is None  # ambiguous search rejected


def test_lint_flags_machine_phrasing():
    assert "uses 'seamless'" in lint("We offer a seamless experience. It is great.")
    assert lint("Acme has built karts in Pune since 2004. Most go to rental tracks, where they take a beating every "
                "weekend. If you run a track, ask about the R8.") == []


# ------------------------------------------------------------------ runner end to end (local repo, fake AI)
class FakeLLM:
    """Answers writer/editor/AI-edit calls like a well-behaved model would."""

    def __init__(self):
        self.calls = []

    async def json(self, *, task, role, system, user, schema, max_tokens=4000, use_cache=True):
        self.calls.append(task)
        if task.startswith("fix.write."):
            return {"text": "Acme Karts builds rental and racing go-karts in Pune, and has done since 2004."}
        if task.startswith("fix.edit."):
            return {"text": "Acme Karts has built rental and racing go-karts in Pune since 2004.",
                    "changes": ["tightened"], "unsupported_claims": []}
        if task == "fix.ai_edit.set_h1":
            return {"edits": [{"search": "<h1></h1>", "replace": "<h1>Go Kart Manufacturer in Pune</h1>"}], "cannot_do": ""}
        if task == "fix.ai_edit.write_opening":
            return {"edits": [{"search": "<h1>Go Kart Manufacturer in Pune</h1>\n",
                               "replace": "<h1>Go Kart Manufacturer in Pune</h1>\n      <p>Acme Karts has built rental "
                                          "and racing go-karts in Pune since 2004.</p>\n"}], "cannot_do": ""}
        if task == "fix.ai_edit.replace_placeholder":
            return {"edits": [{"search": "Lorem ipsum dolor sit amet, consectetur adipiscing elit.",
                               "replace": "© Acme Karts, Pune"}], "cannot_do": ""}
        return {"edits": [], "cannot_do": "not supported in this test"}


def test_runner_prepares_validated_changes_and_publishes(repo_dir):
    repo = LocalRepo(repo_dir)
    fw = run(detect(repo))
    items = build_plan(findings(), PAGES, {"brand": "Acme Karts"})
    ph = next(i for i in items if i.action == "replace_placeholder")
    llm = FakeLLM()
    runner = FixRunner(repo=repo, fw=fw, llm=llm, pages=PAGES, texts={O + "/": "Acme Karts has built go-karts in Pune since 2004."},
                       audit_input={"brand": "Acme Karts"}, answers={ph.questions[0]["id"]: "Our footer: © Acme Karts, Pune"},
                       origin=O)
    result = run(runner.run(items))
    st = {i.action: result["items"][i.id] for i in items if i.kind != "manual"}
    for action in ("set_title", "set_description", "img_attrs", "canonical", "lang", "jsonld", "sitemap", "set_h1",
                   "write_opening", "replace_placeholder"):
        assert st[action]["status"] == "done", (action, st[action])
    files = {f["path"]: f for f in result["files"]}
    assert set(files) == {"index.html", "about.html", "sitemap.xml"} and all(not f["problems"] for f in files.values())
    final = {p: t for c in result["commits"] for p, t in c["files"].items()}
    home = final["index.html"]
    assert "<title>Go Kart Manufacturer in Pune | Acme Karts</title>" in home
    assert 'alt="Acme Karts team in the Pune workshop" width="800" height="600"' in home
    assert '<html lang="en">' in home and '"@type": "Organization"' in home and "Lorem ipsum" not in home
    assert "<h1>Go Kart Manufacturer in Pune</h1>\n      <p>Acme Karts has built" in home
    assert 'alt="Acme Karts team in the Pune workshop"' in final["about.html"]  # same image, other page
    assert st["write_opening"]["text"].startswith("Acme Karts has built")
    assert [c["agent"] for c in result["commits"]][:2] == ["Content Agent", "Search Listing Agent"]
    body = pr_body({"id": 7, "domain": "acme.example"}, items, result)
    assert "Nothing has been merged or deployed" in body and "please read" in body and "Still to do" in body
    where = run(publish(repo, audit={"id": 7, "domain": "acme.example"}, items=items, result=result,
                        branch="seo-inspector/audit-7-fix-1"))
    assert "seo-inspector/audit-7-fix-1" in where
    log = subprocess.run(["git", "-C", str(repo_dir), "log", "--format=%s", "seo-inspector/audit-7-fix-1"],
                         capture_output=True, text=True).stdout
    assert "SEO: Search Listing Agent fixes" in log
    assert (repo_dir / "index.html").read_text() == (FIXTURE / "index.html").read_text()  # main branch untouched


def test_runner_without_ai_skips_writing_but_does_mechanical_fixes(repo_dir):
    repo = LocalRepo(repo_dir)
    items = build_plan(findings(), PAGES, {"brand": "Acme Karts"})
    result = run(FixRunner(repo=repo, fw=run(detect(repo)), llm=None, pages=PAGES, texts={}, audit_input={},
                           answers={}, origin=O).run(items))
    st = {i.action: result["items"][i.id] for i in items if i.kind != "manual"}
    assert st["set_title"]["status"] == "done"
    assert st["write_opening"]["status"] == "skipped" and "AI service" in st["write_opening"]["reason"]
    assert st["replace_placeholder"]["status"] == "skipped" and "needs your answer" in st["replace_placeholder"]["reason"]


# ------------------------------------------------------------------ GitHub client (mocked API)
def test_github_client_commit_and_pull_request():
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append((req.method, req.url.path))
        assert req.headers["authorization"] == "Bearer tok"
        p = req.url.path
        if p.endswith("/git/commits/base"):
            return httpx.Response(200, json={"tree": {"sha": "tree0"}})
        if p.endswith("/git/blobs"):
            return httpx.Response(201, json={"sha": "blob1"})
        if p.endswith("/git/trees"):
            body = json.loads(req.content)
            assert body["base_tree"] == "tree0" and body["tree"][0]["path"] == "index.html"
            return httpx.Response(201, json={"sha": "tree1"})
        if p.endswith("/git/commits"):
            return httpx.Response(201, json={"sha": "c1"})
        if p.endswith("/git/refs"):
            assert json.loads(req.content)["ref"] == "refs/heads/seo/fix"
            return httpx.Response(201, json={})
        if p.endswith("/pulls"):
            return httpx.Response(201, json={"html_url": "https://github.com/o/r/pull/5"})
        return httpx.Response(404)

    async def go():
        client = GitHubClient(parse_repo("https://github.com/o/r"), "tok",
                              client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        sha = await client.commit("base", {"index.html": "<html></html>"}, "msg")
        await client.create_branch("seo/fix", sha)
        return await client.open_pr("seo/fix", "main", "t", "b")
    assert run(go()) == "https://github.com/o/r/pull/5"
    assert ("POST", "/repos/o/r/pulls") in calls
    assert parse_repo("git@github.com:o/r.git").full_name == "o/r"
