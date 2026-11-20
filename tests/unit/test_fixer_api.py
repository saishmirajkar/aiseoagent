"""'Fix with AI' through the API and database: plan → connect repo → prepare → publish (local git repo)."""
import asyncio
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.unit.test_db_queue_api import client  # noqa: F401  (fixture)

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "fixer_site"
O = "https://acme.example"


@pytest.fixture
def local_repo(tmp_path, monkeypatch):
    monkeypatch.setenv("FIXER_ALLOW_LOCAL", "1")
    d = tmp_path / "site"
    shutil.copytree(FIXTURE, d)
    for args in (["init", "-q", "-b", "main"], ["add", "-A"],
                 ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init"]):
        subprocess.run(["git", "-C", str(d), *args], check=True, capture_output=True)
    return d


def _seed_audit() -> int:
    from app.db.models import Audit, Finding, Page, Site, SiteFacts
    from app.db.session import session_scope
    rw = {"type": "rewrite", "titles": [{"text": "Go Kart Manufacturer in Pune | Acme Karts", "fits": True}],
          "descriptions": [{"text": "Rental and racing go-karts built in Pune since 2004.", "fits": True}]}
    with session_scope() as db:
        site = Site(domain="acme-fixer.example")
        db.add(site)
        db.flush()
        a = Audit(site_id=site.id, status="completed", input={"url": O + "/", "brand": "Acme Karts", "language": "en"})
        db.add(a)
        db.flush()
        db.add(SiteFacts(audit_id=a.id, data={"origin": O}))
        db.add(Page(audit_id=a.id, url=O + "/", status_code=200, is_html=True, indexable=True, title="Acme Karts",
                    data={"headings": []}))
        common = dict(audit_id=a.id, category="onpage", impact="Med", effort="Low", priority=2.0, weight=2.0)
        db.add(Finding(module="meta_tags", module_number=3, check_id="meta.title_too_short", status="WARNING",
                       title="Title too short", items=[{"url": O + "/", "evidence": {}, "generated_fix": rw}],
                       urls=[O + "/"], **common))
        db.add(Finding(module="technical", module_number=6, check_id="tech.soft_404", status="FAIL",
                       title="Soft 404s", items=[], urls=[], **common))
        db.flush()
        return a.id


def test_fix_flow_through_api(client, local_repo):  # noqa: F811
    from app.fixer.service import prepare_run, publish_run
    aid = _seed_audit()
    client.post("/api/auth/login", json={"password": "s3cret"})
    plan = client.get(f"/api/audits/{aid}/fix-plan").json()
    assert plan["connection"] is None
    title = next(i for i in plan["items"] if i["action"] == "set_title")
    assert any(i["kind"] == "manual" for i in plan["items"])
    assert client.post(f"/api/audits/{aid}/fixes", json={"selected": [title["id"]]}).status_code == 400  # no repo yet
    r = client.put(f"/api/audits/{aid}/repo", json={"repo": f"local:{local_repo}"})
    assert r.status_code == 200 and r.json()["repo"].startswith("local:")
    run = client.post(f"/api/audits/{aid}/fixes", json={"selected": [title["id"]]}).json()
    assert run["status"] == "queued"
    asyncio.run(prepare_run(run["id"]))
    got = client.get(f"/api/fixes/{run['id']}").json()
    assert got["status"] == "ready", got["error"]
    assert got["result"]["items"][title["id"]]["status"] == "done"
    assert "+    <title>Go Kart Manufacturer in Pune | Acme Karts</title>" in got["result"]["files"][0]["diff"]
    assert client.post(f"/api/fixes/{run['id']}/publish").json()["status"] == "publishing"
    asyncio.run(publish_run(run["id"]))
    done = client.get(f"/api/fixes/{run['id']}").json()
    assert done["status"] == "published" and f"audit-{aid}-fix-{run['id']}" in done["pr_url"]
    assert client.post(f"/api/fixes/{run['id']}/publish").status_code == 409  # can't publish twice


def test_local_repos_need_dev_flag(client, monkeypatch):  # noqa: F811
    monkeypatch.delenv("FIXER_ALLOW_LOCAL", raising=False)
    aid = _seed_audit_for_flag()
    client.post("/api/auth/login", json={"password": "s3cret"})
    r = client.put(f"/api/audits/{aid}/repo", json={"repo": "local:/etc"})
    assert r.status_code == 400 and "development" in r.json()["detail"]
    r = client.put(f"/api/audits/{aid}/repo", json={"repo": "https://github.com/o/r"})
    assert r.status_code == 400 and "token" in r.json()["detail"]


def _seed_audit_for_flag() -> int:
    from app.db.models import Audit, Site
    from app.db.session import session_scope
    with session_scope() as db:
        site = Site(domain="flag.example")
        db.add(site)
        db.flush()
        a = Audit(site_id=site.id, status="completed", input={"url": "https://flag.example/"})
        db.add(a)
        db.flush()
        return a.id


def test_token_encryption_roundtrip(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET", "y" * 32)
    from app.config import env
    env.cache_clear()
    from app.fixer import secrets_box
    enc = secrets_box.encrypt("github_pat_abc")
    assert "github_pat_abc" not in enc and secrets_box.decrypt(enc) == "github_pat_abc"
    monkeypatch.setenv("SESSION_SECRET", "z" * 32)
    env.cache_clear()
    with pytest.raises(secrets_box.SecretUnavailable):
        secrets_box.decrypt(enc)
    env.cache_clear()
