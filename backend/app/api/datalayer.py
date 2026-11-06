"""Data-layer API: Colab inlink import (token auth), watchlist, authority table, outreach tracker."""
from __future__ import annotations

import asyncio
import hmac

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import AuthDep
from app.config import env
from app.datalayer import authority, inlinks
from app.db.models import OutreachProspect
from app.db.session import get_db

router = APIRouter(prefix="/api", tags=["data"])
MAX_SOURCES = 2_000_000


# ------------------------------------------------------------------ Colab import (bearer token)
def require_import_token(authorization: str | None = Header(None)) -> None:
    token = env().import_api_token
    if not token:
        raise HTTPException(503, "IMPORT_API_TOKEN is not set on the server")
    given = (authorization or "").removeprefix("Bearer ").strip()
    if not hmac.compare_digest(given.encode(), token.encode()):
        raise HTTPException(401, "Invalid import token")


class InlinkImportBody(BaseModel):
    release: str = Field(..., pattern=r"^cc-main-[a-z0-9-]+$", max_length=64)
    target_domain: str = Field(..., max_length=255)
    source_domains: list[str] = Field(..., max_length=MAX_SOURCES)


@router.post("/import/inlinks", dependencies=[Depends(require_import_token)])
async def import_inlinks(body: InlinkImportBody) -> dict:
    n = await asyncio.to_thread(inlinks.import_list, body.release, body.target_domain, body.source_domains)
    return {"target_domain": inlinks.norm(body.target_domain), "release": body.release, "stored": n}


@router.get("/import/watchlist", dependencies=[Depends(require_import_token)])
async def import_watchlist() -> dict:
    return {"domains": [w["domain"] for w in await asyncio.to_thread(inlinks.watchlist)]}


# ------------------------------------------------------------------ watchlist (session auth)
class WatchBody(BaseModel):
    domain: str = Field(..., min_length=3, max_length=255)
    kind: str = "client"
    note: str | None = None


@router.get("/watchlist", dependencies=[AuthDep])
async def get_watchlist() -> list[dict]:
    return await asyncio.to_thread(inlinks.watchlist)


@router.post("/watchlist", dependencies=[AuthDep])
async def add_watch(body: WatchBody) -> list[dict]:
    from app.crawler.urls import ensure_scheme, host_of
    domain = host_of(ensure_scheme(body.domain)) or body.domain
    await asyncio.to_thread(inlinks.add_to_watchlist, domain, body.kind, body.note)
    return await asyncio.to_thread(inlinks.watchlist)


@router.delete("/watchlist/{domain}", dependencies=[AuthDep])
async def remove_watch(domain: str) -> list[dict]:
    await asyncio.to_thread(inlinks.remove_from_watchlist, domain)
    return await asyncio.to_thread(inlinks.watchlist)


# ------------------------------------------------------------------ authority table
@router.get("/authority", dependencies=[AuthDep])
async def authority_status() -> dict:
    return await asyncio.to_thread(authority.status)


@router.post("/authority/refresh", dependencies=[AuthDep])
async def authority_refresh() -> dict:
    from app.queue.worker import enqueue_cc_ranks
    job = await asyncio.to_thread(enqueue_cc_ranks, True)
    return {"queued": job is not None, "job_id": job,
            "message": "Refresh queued" if job else "A refresh is already queued or running"}


@router.get("/authority/lookup", dependencies=[AuthDep])
async def authority_lookup(domain: str) -> dict:
    return (await asyncio.to_thread(authority.lookup, [domain]))[domain] or {}


# ------------------------------------------------------------------ outreach tracker
STATUSES = ("new", "contacted", "replied", "won", "lost", "skip")


def _row(p: OutreachProspect) -> dict:
    return {"id": p.id, "site_domain": p.site_domain, "prospect_domain": p.prospect_domain, "url": p.url,
            "title": p.title, "kind": p.kind, "authority": p.authority, "relevance": p.relevance,
            "contact": p.contact, "draft_email": p.draft_email, "status": p.status, "notes": p.notes,
            "updated_at": p.updated_at}


@router.get("/outreach", dependencies=[AuthDep])
def list_outreach(site: str | None = None, db: Session = Depends(get_db)) -> list[dict]:
    q = select(OutreachProspect).order_by(OutreachProspect.site_domain, OutreachProspect.status,
                                          OutreachProspect.relevance.desc().nulls_last())
    if site:
        q = q.where(OutreachProspect.site_domain == site)
    return [_row(p) for p in db.execute(q).scalars().all()]


class OutreachPatch(BaseModel):
    status: str | None = None
    notes: str | None = None
    draft_email: str | None = None


@router.patch("/outreach/{pid}", dependencies=[AuthDep])
def update_outreach(pid: int, body: OutreachPatch, db: Session = Depends(get_db)) -> dict:
    p = db.get(OutreachProspect, pid)
    if p is None:
        raise HTTPException(404, "Prospect not found")
    if body.status is not None:
        if body.status not in STATUSES:
            raise HTTPException(422, f"status must be one of {', '.join(STATUSES)}")
        p.status = body.status
    if body.notes is not None:
        p.notes = body.notes
    if body.draft_email is not None:
        p.draft_email = body.draft_email
    db.flush()
    return _row(p)
