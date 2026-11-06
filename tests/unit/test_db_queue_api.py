"""Tests that need Postgres: job queue, API + auth, settings, retention."""
from __future__ import annotations

import os
from datetime import timedelta

import pytest
from sqlalchemy import select, text

pytestmark = pytest.mark.db


# ------------------------------------------------------------------ job queue
def _clean_jobs(db):
    db.execute(text("DELETE FROM jobs"))
    db.commit()


def test_queue_claim_priority_and_skip_locked(db, database_url):
    from app.db.session import _factory
    from app.queue import jobs
    _clean_jobs(db)
    low = jobs.enqueue(db, "audit", {"n": 1}, priority=0)
    high = jobs.enqueue(db, "audit", {"n": 2}, priority=5)
    db.commit()
    s2 = _factory()()
    try:
        # hold a row lock on the high-priority job in another session: claim must skip it
        s2.execute(text("SELECT id FROM jobs WHERE id = :id FOR UPDATE"), {"id": high.id})
        got = jobs.claim(db, "w1")
        assert got is not None and got.id == low.id
    finally:
        s2.rollback()
        s2.close()
    got2 = jobs.claim(db, "w1")
    assert got2.id == high.id and got2.attempts == 1
    assert jobs.claim(db, "w1") is None


def test_queue_fail_retry_and_exhaust(db, database_url):
    from app.db.models import Job
    from app.queue import jobs
    _clean_jobs(db)
    j = jobs.enqueue(db, "audit", {}, max_attempts=2)
    db.commit()
    c = jobs.claim(db, "w")
    assert jobs.fail(db, c, "boom") == "retry"
    row = db.get(Job, j.id)
    db.refresh(row)
    assert row.status == "queued" and row.run_after > row.created_at  # backoff applied
    db.execute(text("UPDATE jobs SET run_after = now() WHERE id = :id"), {"id": j.id})
    db.commit()
    c2 = jobs.claim(db, "w")
    assert c2.attempts == 2
    assert jobs.fail(db, c2, "boom again") == "failed"


def test_queue_recover_stale_and_release(db, database_url):
    from app.db.models import Job
    from app.queue import jobs
    _clean_jobs(db)
    j = jobs.enqueue(db, "audit", {}, max_attempts=3)
    db.commit()
    jobs.claim(db, "dead-worker")
    db.execute(text("UPDATE jobs SET heartbeat_at = now() - interval '10 minutes' WHERE id = :id"), {"id": j.id})
    db.commit()
    assert jobs.recover_stale(db, timedelta(minutes=3)) == [j.id]
    row = db.get(Job, j.id)
    db.refresh(row)
    assert row.status == "queued"
    c = jobs.claim(db, "w2")
    jobs.release(db, c.id)
    db.refresh(row)
    assert row.status == "queued" and row.attempts == 1


# ------------------------------------------------------------------ API
@pytest.fixture
def client(database_url, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "s3cret")
    monkeypatch.setenv("SESSION_SECRET", "x" * 32)
    monkeypatch.setenv("BLOB_BACKEND", "postgres")
    from fastapi.testclient import TestClient

    from app.config import env
    env.cache_clear()
    from app.main import create_app
    with TestClient(create_app()) as c:
        yield c
    env.cache_clear()


def test_auth_required_and_login(client):
    assert client.get("/api/health").json()["ok"] is True
    assert client.get("/api/audits").status_code == 401
    assert client.get("/api/auth/me").json() == {"authenticated": False, "configured": True}
    assert client.post("/api/auth/login", json={"password": "nope"}).status_code == 401
    assert client.post("/api/auth/login", json={"password": "s3cret"}).status_code == 200
    assert client.get("/api/audits").status_code == 200
    client.post("/api/auth/logout")
    assert client.get("/api/audits").status_code == 401


def test_create_audit_enqueues_job(client, db):
    from app.db.models import Audit, Job
    client.post("/api/auth/login", json={"password": "s3cret"})
    bad = client.post("/api/audits", json={"url": "notaurl at all"})
    assert bad.status_code == 422
    r = client.post("/api/audits", json={"url": "Example.com/shop", "max_pages": 20,
                                         "competitors": ["rival.com"]})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["domain"] == "example.com" and body["status"] == "queued"
    a = db.get(Audit, body["id"])
    assert a.input["url"] == "https://example.com/shop"
    assert a.input["competitors"] == ["https://rival.com/"]
    assert a.settings_snapshot["crawl"]["max_pages"] == 20
    job = db.execute(select(Job).where(Job.payload["audit_id"].astext == str(a.id))).scalar_one()
    assert job.type == "audit" and job.status == "queued"
    detail = client.get(f"/api/audits/{a.id}").json()
    assert detail["progress"]["stage"] == "queued"
    # cancel while queued: audit cancelled and job closed
    assert client.post(f"/api/audits/{a.id}/cancel").json()["status"] == "cancelled"
    db.refresh(job)
    assert job.status == "done"
    assert client.delete(f"/api/audits/{a.id}").status_code == 200


def test_settings_roundtrip_and_validation(client):
    client.post("/api/auth/login", json={"password": "s3cret"})
    s = client.get("/api/settings").json()
    assert s["runtime"]["crawl"]["max_pages"] == 100
    assert s["runtime"]["budgets"]["brave_queries_per_month"] == 1000
    assert s["runtime"]["budgets"]["llm_usd_per_audit"] == 2.0
    assert set(s["integrations"]) >= {"anthropic", "brave_search", "pagespeed", "vertex_ai_gemini"}
    assert all(isinstance(v, bool) for v in s["integrations"].values())  # never values
    r = client.put("/api/settings", json={"crawl": {"rate_limit_per_sec": 5}})
    assert r.status_code == 422  # politeness cap: max 2 req/s
    r = client.put("/api/settings", json={"crawl": {"max_pages": 250}, "scoring": {"category_weights": {"geo": 20}}})
    assert r.status_code == 200
    got = client.get("/api/settings").json()["runtime"]
    assert got["crawl"]["max_pages"] == 250 and got["crawl"]["rate_limit_per_sec"] == 2.0
    assert got["scoring"]["category_weights"]["geo"] == 20
    client.put("/api/settings", json={"crawl": {"max_pages": 100}, "scoring": {"category_weights": {"geo": 10}}})


# ------------------------------------------------------------------ retention
def test_page_text_retention_keeps_last_n(db):
    from app.db.models import Audit, Page, PageText, Site
    from app.sitemodel.persist import apply_page_text_retention
    s = Site(domain=f"retention-{os.getpid()}.test")
    db.add(s)
    db.flush()
    ids = []
    for i in range(5):
        a = Audit(site_id=s.id, status="completed", input={"url": "x"})
        db.add(a)
        db.flush()
        p = Page(audit_id=a.id, url=f"https://x/{i}")
        db.add(p)
        db.flush()
        db.add(PageText(page_id=p.id, text="hello"))
        ids.append(a.id)
    db.flush()
    removed = apply_page_text_retention(db, s.id, 3)
    assert removed == 2
    kept = db.execute(select(Page.audit_id).join(PageText, PageText.page_id == Page.id)
                      .where(Page.audit_id.in_(ids))).scalars().all()
    assert sorted(kept) == ids[2:]


def test_audit_marked_failed_when_job_fails_before_start(db, database_url, monkeypatch):
    import asyncio
    from app.db.models import Audit, Job, Site
    from app.queue import jobs, worker
    db.execute(text("DELETE FROM jobs"))
    s = Site(domain=f"broken-{os.getpid()}.test")
    db.add(s)
    db.flush()
    a = Audit(site_id=s.id, status="queued", input={"url": "https://x.test/"})
    db.add(a)
    db.flush()
    jobs.enqueue(db, "audit", {"audit_id": a.id}, max_attempts=1)
    db.commit()

    async def boom(payload):
        raise ImportError("cannot import name 'x'")
    monkeypatch.setitem(worker.HANDLERS, "audit", boom)
    assert asyncio.run(worker.Worker().run_one()) is True
    db.refresh(a)
    assert a.status == "failed" and "ImportError" in a.error
