"""Audit API: create, list, progress, findings, pages, reports, cancel, delete."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, or_, select, text, update
from sqlalchemy.orm import Session

from app.auth import AuthDep
from app.crawler.urls import ensure_scheme, host_of, is_public_style_host, normalize_url, site_key
from app.db.models import Audit, Finding, Image, Link, Page, PageText, Site
from app.db.session import get_db
from app.queue import jobs
from app.reports.build import STATUS_ORDER, finding_dict, top_fixes
from app.settings_store import load as load_settings
from app.storage.blobs import blob_store

router = APIRouter(prefix="/api", tags=["audits"], dependencies=[AuthDep])


class AuditCreate(BaseModel):
    url: str
    max_pages: int | None = Field(None, ge=1, le=5000)
    business_type: str | None = None
    country: str | None = None
    language: str | None = None
    keywords: list[str] = Field(default_factory=list, max_length=50)
    competitors: list[str] = Field(default_factory=list, max_length=3)
    business_name: str | None = None
    address: str | None = None
    phone: str | None = None
    brand: str | None = None

    @field_validator("url")
    @classmethod
    def _url(cls, v: str) -> str:
        n = normalize_url(ensure_scheme(v))
        if not n or not is_public_style_host(host_of(n)):
            raise ValueError("Enter a valid http(s) website URL")
        return n

    @field_validator("competitors")
    @classmethod
    def _comp(cls, v: list[str]) -> list[str]:
        out = []
        for c in v:
            if c.strip():
                n = normalize_url(ensure_scheme(c))
                if not n:
                    raise ValueError(f"Invalid competitor URL: {c}")
                out.append(n)
        return out


def _audit_row(a: Audit, domain: str | None) -> dict[str, Any]:
    return {"id": a.id, "domain": domain, "url": a.input.get("url"), "status": a.status,
            "overall": (a.scores or {}).get("overall"), "coverage": a.coverage,
            "created_at": a.created_at, "started_at": a.started_at, "finished_at": a.finished_at,
            "error": a.error}


def _get_audit(db: Session, audit_id: int) -> Audit:
    a = db.get(Audit, audit_id)
    if a is None:
        raise HTTPException(404, "Audit not found")
    return a


@router.post("/audits", status_code=201)
def create_audit(body: AuditCreate, db: Session = Depends(get_db)) -> dict:
    domain = site_key(host_of(body.url))
    site = db.execute(select(Site).where(Site.domain == domain)).scalar_one_or_none()
    if site is None:
        site = Site(domain=domain)
        db.add(site)
        db.flush()
    settings = load_settings(db)
    if body.max_pages:
        settings.crawl.max_pages = body.max_pages
    a = Audit(site_id=site.id, status="queued", input=body.model_dump(), settings_snapshot=settings.model_dump(),
              progress={"stage": "queued", "message": "Waiting for the worker"})
    db.add(a)
    db.flush()
    jobs.enqueue(db, "audit", {"audit_id": a.id}, max_attempts=2)
    return _audit_row(a, domain)


@router.get("/audits")
def list_audits(domain: str | None = None, limit: int = Query(50, le=500), db: Session = Depends(get_db)) -> list[dict]:
    q = select(Audit, Site.domain).join(Site, Site.id == Audit.site_id).order_by(Audit.id.desc()).limit(limit)
    if domain:
        q = q.where(Site.domain == domain)
    return [_audit_row(a, d) for a, d in db.execute(q).all()]


@router.get("/sites")
def list_sites(db: Session = Depends(get_db)) -> list[dict]:
    rows = db.execute(select(Site.domain, func.count(Audit.id), func.max(Audit.created_at))
                      .join(Audit, Audit.site_id == Site.id, isouter=True)
                      .group_by(Site.domain).order_by(func.max(Audit.created_at).desc().nulls_last())).all()
    return [{"domain": d, "audits": n, "last_audit": t} for d, n, t in rows]


@router.get("/audits/{audit_id}")
def get_audit(audit_id: int, db: Session = Depends(get_db)) -> dict:
    a = _get_audit(db, audit_id)
    site = db.get(Site, a.site_id)
    out = _audit_row(a, site.domain if site else None)
    out.update({"input": a.input, "progress": a.progress, "scores": a.scores, "summary": a.summary,
                "cost": a.cost, "reports": sorted(k for k in (a.report_keys or {}) if k != "error"),
                "report_error": (a.report_keys or {}).get("error")})
    if a.status == "completed":
        findings = db.execute(select(Finding).where(Finding.audit_id == audit_id)).scalars().all()
        out["top_fixes"] = [finding_dict(f) for f in top_fixes(findings)]
        out["missing"] = (a.summary or {}).get("missing", [])
    return out


@router.get("/audits/{audit_id}/findings")
def get_findings(audit_id: int, category: str | None = None, status: str | None = None,
                 module: str | None = None, q: str | None = None, db: Session = Depends(get_db)) -> list[dict]:
    _get_audit(db, audit_id)
    stmt = select(Finding).where(Finding.audit_id == audit_id)
    if category:
        stmt = stmt.where(Finding.category == category)
    if status:
        stmt = stmt.where(Finding.status.in_(status.split(",")))
    if module:
        stmt = stmt.where(Finding.module == module)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(Finding.title.ilike(like), Finding.summary.ilike(like), Finding.check_id.ilike(like),
                              text("array_to_string(findings.urls, ' ') ILIKE :like").bindparams(like=like)))
    rows = db.execute(stmt).scalars().all()
    rows.sort(key=lambda f: (STATUS_ORDER.get(f.status, 9), -f.priority, f.module_number))
    return [finding_dict(f) for f in rows]


@router.get("/audits/{audit_id}/pages")
def get_pages(audit_id: int, q: str | None = None, only: str | None = None,
              limit: int = Query(200, le=2000), offset: int = 0, db: Session = Depends(get_db)) -> dict:
    _get_audit(db, audit_id)
    stmt = select(Page).where(Page.audit_id == audit_id)
    if q:
        stmt = stmt.where(or_(Page.url.ilike(f"%{q}%"), Page.title.ilike(f"%{q}%")))
    if only == "errors":
        stmt = stmt.where(or_(Page.status_code >= 400, Page.status_code.is_(None)))
    elif only == "nonindexable":
        stmt = stmt.where(Page.indexable.is_(False))
    total = db.execute(select(func.count()).select_from(stmt.subquery())).scalar()
    rows = db.execute(stmt.order_by(Page.depth.nulls_last(), Page.url).limit(limit).offset(offset)).scalars().all()
    issue_counts = dict(db.execute(text("""
        SELECT u, count(*) FROM findings, unnest(urls) u
        WHERE audit_id = :a AND status IN ('FAIL','WARNING') GROUP BY u"""), {"a": audit_id}).all())
    return {"total": total, "pages": [{
        "id": p.id, "url": p.url, "status": p.status_code, "title": p.title, "depth": p.depth,
        "inlinks": p.inlinks, "word_count": p.word_count, "indexable": p.indexable, "in_sitemap": p.in_sitemap,
        "rendered": p.rendered, "js_dependent": p.js_dependent, "response_ms": p.response_ms,
        "issues": issue_counts.get(p.url, 0)} for p in rows]}


@router.get("/audits/{audit_id}/pages/{page_id}")
def get_page(audit_id: int, page_id: int, db: Session = Depends(get_db)) -> dict:
    p = db.get(Page, page_id)
    if p is None or p.audit_id != audit_id:
        raise HTTPException(404, "Page not found")
    findings = db.execute(select(Finding).where(Finding.audit_id == audit_id, Finding.urls.any(p.url))).scalars().all()
    page_findings = []
    for f in sorted(findings, key=lambda f: (STATUS_ORDER.get(f.status, 9), -f.priority)):
        d = finding_dict(f)
        d["items"] = [i for i in f.items if i.get("url") == p.url]
        page_findings.append(d)
    images = db.execute(select(Image).where(Image.page_id == page_id).limit(300)).scalars().all()
    links = db.execute(select(Link).where(Link.source_page_id == page_id).limit(500)).scalars().all()
    inlinks = db.execute(select(Page.url, Link.anchor).join(Link, Link.source_page_id == Page.id)
                         .where(Link.audit_id == audit_id, Link.target_url == p.url).limit(200)).all()
    pt = db.get(PageText, page_id)
    return {
        "id": p.id, "url": p.url, "status": p.status_code, "data": p.data, "findings": page_findings,
        "images": [{c: getattr(i, c) for c in ("src", "alt", "has_alt", "filename", "width_attr", "height_attr",
                                              "rendered_w", "rendered_h", "intrinsic_w", "intrinsic_h", "bytes",
                                              "format", "loading")} for i in images],
        "links": [{"target": l.target_url, "anchor": l.anchor, "internal": l.internal, "nofollow": l.nofollow,
                   "sponsored": l.sponsored, "ugc": l.ugc, "status": l.target_status} for l in links],
        "inlinks": [{"source": u, "anchor": a} for u, a in inlinks],
        "text_excerpt": pt.text[:3000] if pt else None,
        "text_retained": pt is not None,
    }


_CT = {"html": "text/html; charset=utf-8", "json": "application/json", "csv": "text/csv; charset=utf-8"}


@router.get("/audits/{audit_id}/report.{fmt}")
def get_report(audit_id: int, fmt: str, download: bool = False, db: Session = Depends(get_db)) -> Response:
    if fmt not in _CT:
        raise HTTPException(404, "Unknown format")
    a = _get_audit(db, audit_id)
    key = (a.report_keys or {}).get(fmt)
    data = blob_store().get(key) if key else None
    if data is None:
        raise HTTPException(404, "Report not available")
    headers = {}
    if download or fmt != "html":
        headers["Content-Disposition"] = f'attachment; filename="seo-audit-{audit_id}.{fmt}"'
    return Response(content=data, media_type=_CT[fmt], headers=headers)


@router.post("/audits/{audit_id}/cancel")
def cancel_audit(audit_id: int, db: Session = Depends(get_db)) -> dict:
    a = _get_audit(db, audit_id)
    if a.status == "queued":
        a.status = "cancelled"
        db.execute(text("UPDATE jobs SET status = 'done', finished_at = now(), last_error = 'cancelled' "
                        "WHERE type = 'audit' AND status = 'queued' AND (payload->>'audit_id')::int = :id"),
                   {"id": audit_id})
    elif a.status == "running":
        a.status = "cancel_requested"
    return {"id": a.id, "status": a.status}


@router.delete("/audits/{audit_id}")
def delete_audit(audit_id: int, db: Session = Depends(get_db)) -> dict:
    a = _get_audit(db, audit_id)
    if a.status in ("running", "cancel_requested"):
        raise HTTPException(409, "Cancel the audit before deleting it")
    db.execute(text("UPDATE jobs SET status = 'done' WHERE type = 'audit' AND status = 'queued' "
                    "AND (payload->>'audit_id')::int = :id"), {"id": audit_id})
    db.delete(a)
    blob_store().delete_prefix(f"reports/{audit_id}/")
    return {"deleted": audit_id}
