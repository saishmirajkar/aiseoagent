"""Database glue for 'Fix with AI': plans, repository connections and the prepare/publish jobs."""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Audit, Finding, FixRun, Page, PageText, RepoConnection, Site, SiteFacts
from app.db.session import session_scope
from app.fixer import secrets_box
from app.fixer.detect import detect
from app.fixer.github import GitHubClient, GitHubError, parse_repo
from app.fixer.plan import FixItem, build_plan
from app.fixer.repo import GitHubRepo, LocalRepo, Repo
from app.fixer.runner import FixRunner, publish
from app.reports.build import finding_dict

log = logging.getLogger(__name__)


def local_repos_allowed() -> bool:
    """Local checkouts (repo 'local:/path') are for development and tests only."""
    return os.environ.get("FIXER_ALLOW_LOCAL") == "1"


def audit_context(db: Session, audit_id: int) -> dict[str, Any]:
    audit = db.get(Audit, audit_id)
    if audit is None:
        raise KeyError(audit_id)
    site = db.get(Site, audit.site_id)
    facts = (db.get(SiteFacts, audit_id).data if db.get(SiteFacts, audit_id) else {}) or {}
    findings = [finding_dict(f) for f in db.execute(select(Finding).where(Finding.audit_id == audit_id)).scalars()]
    rows = db.execute(select(Page).where(Page.audit_id == audit_id)).scalars().all()
    pages = [{"url": p.url, "title": p.title, "meta_description": p.meta_description, "indexable": p.indexable,
              "status_code": p.status_code, "data": p.data or {}} for p in rows]
    texts = dict(db.execute(select(Page.url, PageText.text).join(PageText, PageText.page_id == Page.id)
                            .where(Page.audit_id == audit_id)).all())
    return {"audit": {"id": audit.id, "status": audit.status, "domain": site.domain if site else None,
                      "url": audit.input.get("url")},
            "site_id": audit.site_id, "input": audit.input, "origin": facts.get("origin") or audit.input.get("url", ""),
            "findings": findings, "pages": pages, "texts": texts}


def plan_items(ctx: dict[str, Any]) -> list[FixItem]:
    return build_plan(ctx["findings"], ctx["pages"], ctx["input"])


def connection_info(db: Session, site_id: int) -> dict[str, Any] | None:
    c = db.execute(select(RepoConnection).where(RepoConnection.site_id == site_id)).scalar_one_or_none()
    if c is None:
        return None
    return {"repo": c.repo, "default_branch": c.default_branch, "has_token": bool(c.token_enc),
            "updated_at": c.updated_at.isoformat() if c.updated_at else None}


async def verify_and_save(site_id: int, repo_value: str, token: str | None) -> dict[str, Any]:
    """Check the repository is reachable with this token, then store it (token encrypted)."""
    if repo_value.startswith("local:"):
        if not local_repos_allowed():
            raise GitHubError("Local repositories are only allowed in development (FIXER_ALLOW_LOCAL=1)")
        repo = LocalRepo(repo_value[len("local:"):])
        name, branch, enc = repo_value, repo.default_branch, None
    else:
        if not token:
            raise GitHubError("Paste a GitHub access token for this repository")
        ref = parse_repo(repo_value)
        client = GitHubClient(ref, token)
        try:
            info = await client.repo_info()
        finally:
            await client.close()
        perms = info.get("permissions") or {}
        if perms and not perms.get("push"):
            raise GitHubError("This token can read the repository but not write to it. Give it 'Contents: read and "
                              "write' and 'Pull requests: read and write'.")
        name, branch, enc = ref.full_name, info.get("default_branch"), secrets_box.encrypt(token)

    def save() -> dict:
        with session_scope() as db:
            c = db.execute(select(RepoConnection).where(RepoConnection.site_id == site_id)).scalar_one_or_none()
            if c is None:
                c = RepoConnection(site_id=site_id, repo=name)
                db.add(c)
            c.repo, c.default_branch = name, branch
            if enc is not None or name.startswith("local:"):
                c.token_enc = enc
            db.flush()
            return connection_info(db, site_id)
    return await asyncio.to_thread(save)


async def open_repo(site_id: int) -> Repo:
    def load() -> tuple[str, str | None]:
        with session_scope() as db:
            c = db.execute(select(RepoConnection).where(RepoConnection.site_id == site_id)).scalar_one_or_none()
            if c is None:
                raise GitHubError("Connect a GitHub repository first")
            return c.repo, c.token_enc
    name, enc = await asyncio.to_thread(load)
    if name.startswith("local:"):
        if not local_repos_allowed():
            raise GitHubError("Local repositories are only allowed in development")
        return LocalRepo(name[len("local:"):])
    if not enc:
        raise GitHubError("No access token stored for this repository; reconnect it")
    return await GitHubRepo.open(GitHubClient(parse_repo(name), secrets_box.decrypt(enc)))


def _update(run_id: int, **values) -> None:
    with session_scope() as db:
        run = db.get(FixRun, run_id)
        for k, v in values.items():
            setattr(run, k, v)


async def prepare_run(run_id: int) -> None:
    def load():
        with session_scope() as db:
            run = db.get(FixRun, run_id)
            run.status, run.error = "preparing", None
            ctx = audit_context(db, run.audit_id)
            from app.settings_store import load as load_settings
            return ctx, list(run.selection), dict(run.answers), load_settings(db)
    ctx, selection, answers, settings = await asyncio.to_thread(load)
    try:
        items = [i for i in plan_items(ctx) if i.id in set(selection)]
        repo = await open_repo(ctx["site_id"])
        fw = await detect(repo)
        from app.llm.client import make_llm
        llm = make_llm(settings, ctx["audit"]["id"])
        runner = FixRunner(repo=repo, fw=fw, llm=llm, pages=ctx["pages"], texts=ctx["texts"],
                           audit_input=ctx["input"], answers=answers, origin=ctx["origin"])
        result = await runner.run(items)
        if hasattr(repo, "client"):
            await repo.client.close()
        await asyncio.to_thread(_update, run_id, status="ready", result=result)
    except Exception as e:
        log.exception("fix run %s failed", run_id)
        await asyncio.to_thread(_update, run_id, status="failed", error=f"{type(e).__name__}: {e}"[:2000])


async def publish_run(run_id: int) -> None:
    def load():
        with session_scope() as db:
            run = db.get(FixRun, run_id)
            run.status = "publishing"
            return audit_context(db, run.audit_id), list(run.selection), dict(run.result)
    ctx, selection, result = await asyncio.to_thread(load)
    try:
        items = [i for i in plan_items(ctx) if i.id in set(selection) or i.kind == "manual"]
        repo = await open_repo(ctx["site_id"])
        branch = f"seo-inspector/audit-{ctx['audit']['id']}-fix-{run_id}"
        url = await publish(repo, audit=ctx["audit"], items=items, result=result, branch=branch)
        if hasattr(repo, "client"):
            await repo.client.close()
        await asyncio.to_thread(_update, run_id, status="published", pr_url=url)
    except Exception as e:
        log.exception("publishing fix run %s failed", run_id)
        await asyncio.to_thread(_update, run_id, status="ready", error=f"Publishing failed: {e}"[:2000])
