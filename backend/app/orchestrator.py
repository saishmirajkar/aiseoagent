"""The single audit pipeline: crawl once -> Site Model -> check modules -> score -> report."""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import insert, select, update

from app.config import RuntimeSettings, env
from app.crawler.crawl import CrawlCancelled, CrawlError, crawl_site
from app.db.models import Audit, Finding, Metric, ModuleRun
from app.db.session import session_scope
from app.modules.base import AuditContext, AuditInput, CheckModule, CheckResult
from app.modules.common import MAX_ITEMS
from app.modules.registry import all_modules, run_module
from app.reports.build import build_report_data, missing_summary, render_csv, render_html, render_json, top_fixes
from app.scoring.scorer import check_score, compute, priority
from app.sitemodel.persist import apply_page_text_retention, clear_audit_data, save_site_model
from app.storage.blobs import blob_store

log = logging.getLogger(__name__)
MAX_URLS_PER_FINDING = 2000


class ProgressWriter:
    """Holds the audit's progress JSON and writes it to Postgres (throttled)."""

    def __init__(self, audit_id: int, modules: list[CheckModule]):
        self.audit_id = audit_id
        self.state: dict[str, Any] = {
            "stage": "queued", "message": "", "crawl": {},
            "modules": {m.key: {"number": m.number, "name": m.name, "status": "pending",
                                "planned_phase": m.planned_phase} for m in modules},
        }
        self._last = 0.0

    async def write(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last < 1.0:
            return
        self._last = now
        self.state["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        state = dict(self.state)

        def _w() -> None:
            with session_scope() as db:
                db.execute(update(Audit).where(Audit.id == self.audit_id).values(progress=state))
        await asyncio.to_thread(_w)

    async def crawl(self, info: dict) -> None:
        self.state["stage"] = info.get("stage", "crawl")
        self.state["message"] = info.get("message", "")
        self.state["crawl"] = {k: v for k, v in info.items() if k not in ("stage", "message")}
        await self.write()

    async def stage(self, stage: str, message: str = "") -> None:
        self.state["stage"], self.state["message"] = stage, message
        await self.write(force=True)

    async def module(self, key: str, status: str) -> None:
        self.state["modules"][key]["status"] = status
        self.state["stage"] = "modules"
        self.state["message"] = f"{self.state['modules'][key]['name']}: {status}"
        await self.write(force=True)


def _finding_rows(audit_id: int, module: CheckModule, results: list[CheckResult]) -> list[dict]:
    rows = []
    for r in results:
        items = [i.as_dict() for i in r.items]
        urls = sorted({u for i in r.items for u in [i.url, *i.also_on] if u})[:MAX_URLS_PER_FINDING]
        label = None
        if r.spec.missing_label and r.status.value in ("FAIL", "WARNING"):
            label = r.spec.missing_label
        rows.append({
            "audit_id": audit_id, "module": module.key, "module_number": module.number,
            "check_id": r.spec.id, "category": module.category.value, "status": r.status.value,
            "title": r.spec.title, "summary": r.summary or None, "why": r.spec.why, "fix": r.spec.fix,
            "reason": r.reason, "impact": (r.impact or r.spec.impact).value, "effort": r.spec.effort.value,
            "priority": priority(r), "weight": r.spec.score_weight, "score": check_score(r),
            "affected": r.affected, "total": r.total, "items": items[:MAX_ITEMS],
            "items_truncated": max(0, len(items) - MAX_ITEMS), "urls": urls,
            "generated_fix": r.generated_fix, "sources": r.sources, "missing_label": label,
        })
    return rows


def _load(audit_id: int) -> tuple[Audit, RuntimeSettings | None]:
    with session_scope() as db:
        a = db.get(Audit, audit_id)
        if a is None:
            raise KeyError(f"audit {audit_id} not found")
        if a.status == "cancelled":
            db.expunge(a)
            return a, None
        settings = RuntimeSettings.model_validate(a.settings_snapshot or {})
        clear_audit_data(db, audit_id)
        a.status = "running"
        a.started_at = datetime.now(timezone.utc)
        a.error = None
        db.flush()
        db.expunge(a)
        return a, settings


def _set_status(audit_id: int, status: str, error: str | None = None, **values) -> None:
    with session_scope() as db:
        db.execute(update(Audit).where(Audit.id == audit_id).values(
            status=status, error=error, finished_at=datetime.now(timezone.utc), **values))


async def run_audit(audit_id: int, *, verify_tls: bool = True) -> str:
    """Run a whole audit. Never raises for site problems; returns the final status."""
    audit, settings = await asyncio.to_thread(_load, audit_id)
    if settings is None:
        return "cancelled"
    modules = all_modules()
    progress = ProgressWriter(audit_id, modules)
    inputs = AuditInput(**{k: v for k, v in audit.input.items() if k in AuditInput.__dataclass_fields__})

    async def cancelled() -> bool:
        def _q() -> bool:
            with session_scope() as db:
                return db.execute(select(Audit.status).where(Audit.id == audit_id)).scalar() == "cancel_requested"
        return await asyncio.to_thread(_q)

    await progress.stage("crawl", "Starting crawl")
    try:
        site = await crawl_site(inputs.url, settings.crawl, env().user_agent, progress=progress.crawl,
                                cancelled=cancelled, verify_tls=verify_tls)
    except CrawlCancelled:
        await asyncio.to_thread(_set_status, audit_id, "cancelled", None, progress={**progress.state, "stage": "cancelled"})
        return "cancelled"
    except CrawlError as e:
        await asyncio.to_thread(_set_status, audit_id, "failed", str(e), progress={**progress.state, "stage": "failed"})
        return "failed"

    await progress.stage("saving", "Saving the Site Model")

    def _save() -> None:
        with session_scope() as db:
            save_site_model(db, audit_id, site)
    await asyncio.to_thread(_save)

    ctx = AuditContext(audit_id=audit_id, site=site, settings=settings, inputs=inputs,
                       integrations=env().integration_status())
    for m in modules:
        if await cancelled():
            await asyncio.to_thread(_set_status, audit_id, "cancelled", None)
            return "cancelled"
        await progress.module(m.key, "running")
        results, status, error, ms = await run_module(m, ctx)
        rows = _finding_rows(audit_id, m, results)
        measured = [r for r in results if r.status.value in ("PASS", "WARNING", "FAIL")]
        applicable = [r for r in results if r.status.value != "N/A"]
        app_w = sum(r.spec.score_weight for r in applicable)
        cov = round(100 * sum(r.spec.score_weight for r in measured) / app_w, 1) if app_w else None

        def _persist(rows=rows, status=status, error=error, ms=ms, cov=cov, key=m.key) -> None:
            with session_scope() as db:
                if rows:
                    db.execute(insert(Finding), rows)
                db.add(ModuleRun(audit_id=audit_id, module=key, status=status, coverage=cov,
                                 duration_ms=ms, error=error))
        await asyncio.to_thread(_persist)
        await progress.module(m.key, status)

    await progress.stage("scoring", "Scoring")
    card = compute(modules, ctx.results, settings.scoring.category_weights)
    metrics = ctx.shared.get("metrics", [])

    def _finish() -> None:
        with session_scope() as db:
            if metrics:
                db.execute(insert(Metric), [{"audit_id": audit_id, "module": m, "key": k, "value": v, "source": s}
                                            for m, k, v, s in metrics])
            findings = db.execute(select(Finding).where(Finding.audit_id == audit_id)).scalars().all()
            counts: dict[str, int] = {}
            for f in findings:
                counts[f.status] = counts.get(f.status, 0) + 1
            summary = {
                "status_counts": counts,
                "missing": missing_summary(findings),
                "top_fixes": [f.id for f in top_fixes(findings)],
                "crawl": site.facts.crawl,
                "pages": len(site.pages), "html_pages": len(site.html_pages()),
                "indexable_pages": len(site.indexable_pages()),
            }
            db.execute(update(Audit).where(Audit.id == audit_id).values(
                scores={"overall": card.overall, "categories": card.categories, "modules": card.modules},
                coverage=card.coverage, summary=summary,
                cost={"llm_usd": 0.0, "search_queries": 0, "pagespeed_calls": 0,
                      "note": "No paid API calls are made in this build phase"}))
    await asyncio.to_thread(_finish)

    await progress.stage("report", "Rendering reports")

    def _report() -> dict:
        with session_scope() as db:
            data = build_report_data(db, audit_id)
        store = blob_store()
        keys = {}
        for fmt, content, ctype in (("html", render_html(data), "text/html; charset=utf-8"),
                                    ("json", render_json(data), "application/json"),
                                    ("csv", render_csv(data), "text/csv; charset=utf-8")):
            key = f"reports/{audit_id}/report.{fmt}"
            store.put(key, content.encode("utf-8"), ctype)
            keys[fmt] = key
        return keys
    try:
        keys = await asyncio.to_thread(_report)
    except Exception as e:  # report rendering must not lose the audit
        log.exception("Report rendering failed for audit %s", audit_id)
        keys = {"error": f"{type(e).__name__}: {e}"}

    final_progress = {**progress.state, "stage": "done", "message": "Audit complete"}

    def _complete() -> None:
        with session_scope() as db:
            db.execute(update(Audit).where(Audit.id == audit_id).values(
                status="completed", finished_at=datetime.now(timezone.utc), report_keys=keys,
                progress=final_progress))
            apply_page_text_retention(db, audit.site_id, settings.retention.page_text_audits_per_site)
    await asyncio.to_thread(_complete)
    return "completed"
