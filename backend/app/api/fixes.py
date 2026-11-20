"""'Fix with AI' API: plan, repository connection, prepare (diff preview) and publish (pull request)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import AuthDep
from app.db.models import Audit, FixRun, RepoConnection
from app.db.session import get_db
from app.fixer.github import GitHubError
from app.fixer.secrets_box import SecretUnavailable
from app.fixer.service import audit_context, connection_info, plan_items, verify_and_save
from app.queue import jobs

router = APIRouter(prefix="/api", tags=["fixes"], dependencies=[AuthDep])


class ConnectBody(BaseModel):
    repo: str = Field(..., max_length=300)
    token: str | None = Field(None, max_length=300)


class FixRequest(BaseModel):
    selected: list[str] = Field(default_factory=list, max_length=1000)
    answers: dict[str, str] = Field(default_factory=dict)


def _audit(db: Session, audit_id: int) -> Audit:
    a = db.get(Audit, audit_id)
    if a is None:
        raise HTTPException(404, "Audit not found")
    if a.status != "completed":
        raise HTTPException(409, "The audit hasn't finished yet")
    return a


def _run_json(run: FixRun) -> dict:
    return {"id": run.id, "audit_id": run.audit_id, "status": run.status, "selection": run.selection,
            "answers": run.answers, "result": run.result, "pr_url": run.pr_url, "error": run.error,
            "created_at": run.created_at.isoformat() if run.created_at else None}


@router.get("/audits/{audit_id}/fix-plan")
def get_plan(audit_id: int, db: Session = Depends(get_db)) -> dict:
    a = _audit(db, audit_id)
    ctx = audit_context(db, audit_id)
    items = [i.to_json() for i in plan_items(ctx)]
    runs = db.execute(select(FixRun).where(FixRun.audit_id == audit_id).order_by(FixRun.id.desc())).scalars().all()
    return {"connection": connection_info(db, a.site_id), "items": items, "runs": [_run_json(r) for r in runs[:10]]}


@router.put("/audits/{audit_id}/repo")
async def connect_repo(audit_id: int, body: ConnectBody, db: Session = Depends(get_db)) -> dict:
    a = _audit(db, audit_id)
    try:
        return await verify_and_save(a.site_id, body.repo.strip(), (body.token or "").strip() or None)
    except (GitHubError, SecretUnavailable) as e:
        raise HTTPException(400, str(e)) from e


@router.delete("/audits/{audit_id}/repo")
def disconnect_repo(audit_id: int, db: Session = Depends(get_db)) -> dict:
    a = _audit(db, audit_id)
    c = db.execute(select(RepoConnection).where(RepoConnection.site_id == a.site_id)).scalar_one_or_none()
    if c is not None:
        db.delete(c)
    return {"ok": True}


@router.post("/audits/{audit_id}/fixes")
def start_fix(audit_id: int, body: FixRequest, db: Session = Depends(get_db)) -> dict:
    a = _audit(db, audit_id)
    if connection_info(db, a.site_id) is None:
        raise HTTPException(400, "Connect a GitHub repository first")
    if not body.selected:
        raise HTTPException(400, "Choose at least one fix")
    run = FixRun(audit_id=audit_id, selection=body.selected, answers=body.answers)
    db.add(run)
    db.flush()
    jobs.enqueue(db, "fix_prepare", {"run_id": run.id}, max_attempts=1)
    return _run_json(run)


@router.get("/fixes/{run_id}")
def get_fix(run_id: int, db: Session = Depends(get_db)) -> dict:
    run = db.get(FixRun, run_id)
    if run is None:
        raise HTTPException(404, "Fix run not found")
    return _run_json(run)


@router.post("/fixes/{run_id}/publish")
def publish_fix(run_id: int, db: Session = Depends(get_db)) -> dict:
    run = db.get(FixRun, run_id)
    if run is None:
        raise HTTPException(404, "Fix run not found")
    if run.status != "ready":
        raise HTTPException(409, f"This run is {run.status}; only a prepared run can be published")
    if not (run.result or {}).get("commits"):
        raise HTTPException(409, "There are no changes to publish")
    run.status = "publishing"
    jobs.enqueue(db, "fix_publish", {"run_id": run.id}, max_attempts=1)
    return _run_json(run)
