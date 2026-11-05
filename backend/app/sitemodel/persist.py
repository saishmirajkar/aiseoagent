"""Write the in-memory Site Model to Postgres (bulk inserts)."""
from __future__ import annotations

from sqlalchemy import delete, insert, select, text
from sqlalchemy.orm import Session

from app.db.models import Audit, Finding, Image, Link, Metric, ModuleRun, Page, PageText, SiteFacts
from app.sitemodel.types import SiteModel

CHUNK = 2000


def clear_audit_data(db: Session, audit_id: int) -> None:
    """Remove partial data from an earlier attempt of the same audit."""
    for model in (Finding, Metric, ModuleRun, Image, Link, SiteFacts):
        db.execute(delete(model).where(model.audit_id == audit_id))
    db.execute(delete(Page).where(Page.audit_id == audit_id))


def _chunks(rows: list[dict]):
    for i in range(0, len(rows), CHUNK):
        yield rows[i:i + CHUNK]


def save_site_model(db: Session, audit_id: int, site: SiteModel) -> dict[str, int]:
    page_rows = []
    for p in site.pages.values():
        page_rows.append({
            "audit_id": audit_id, "url": p.url, "final_url": p.final_url, "status_code": p.status,
            "is_html": p.is_html, "indexable": p.indexable, "depth": p.depth, "title": p.title,
            "meta_description": p.meta_description, "word_count": p.word_count if p.is_ok else None,
            "response_ms": p.response_ms, "in_sitemap": p.in_sitemap, "inlinks": p.inlinks,
            "template_key": p.template_key, "rendered": p.rendered, "js_dependent": p.js_dependent,
            "content_hash": p.content_hash, "simhash": p.simhash, "data": p.to_json(),
        })
    ids: dict[str, int] = {}
    for chunk in _chunks(page_rows):
        res = db.execute(insert(Page).returning(Page.id, Page.url), chunk)
        ids.update({url: pid for pid, url in res.all()})

    texts = [{"page_id": ids[p.url], "text": p.text} for p in site.pages.values() if p.text]
    for chunk in _chunks(texts):
        db.execute(insert(PageText), chunk)

    link_rows, img_rows = [], []
    for p in site.pages.values():
        pid = ids[p.url]
        for l in p.links:
            link_rows.append({"audit_id": audit_id, "source_page_id": pid, "target_url": l.target,
                              "anchor": l.anchor, "internal": l.internal, "nofollow": l.nofollow,
                              "sponsored": l.sponsored, "ugc": l.ugc,
                              "target_status": site.final_status_of(l.target)})
        for i in p.images:
            a = site.images.get(i.src)
            img_rows.append({"audit_id": audit_id, "page_id": pid, "src": i.src, "alt": i.alt,
                             "has_alt": i.alt is not None, "filename": i.filename, "width_attr": i.width_attr,
                             "height_attr": i.height_attr, "rendered_w": i.rendered_w, "rendered_h": i.rendered_h,
                             "intrinsic_w": a.intrinsic_w if a else None, "intrinsic_h": a.intrinsic_h if a else None,
                             "bytes": a.bytes if a else None, "format": a.format if a else None,
                             "loading": i.loading})
    for chunk in _chunks(link_rows):
        db.execute(insert(Link), chunk)
    for chunk in _chunks(img_rows):
        db.execute(insert(Image), chunk)

    facts = dict(site.facts.__dict__)
    facts["url_status"] = {u: s.__dict__ for u, s in list(site.url_status.items())[:5000]}
    facts["duplicate_groups"] = site.duplicate_groups[:200]
    db.add(SiteFacts(audit_id=audit_id, data=facts))
    db.flush()
    return ids


def apply_page_text_retention(db: Session, site_id: int, keep: int) -> int:
    """Keep full page text only for the most recent ``keep`` completed audits of a site."""
    keep_ids = db.execute(select(Audit.id).where(Audit.site_id == site_id, Audit.status == "completed")
                          .order_by(Audit.id.desc()).limit(keep)).scalars().all()
    res = db.execute(text("""
        DELETE FROM page_texts pt USING pages p, audits a
        WHERE pt.page_id = p.id AND p.audit_id = a.id AND a.site_id = :s
          AND a.status = 'completed' AND NOT (a.id = ANY(:keep))
    """), {"s": site_id, "keep": list(keep_ids)})
    return res.rowcount or 0
