"""Assemble report data from the database and render HTML / JSON / CSV."""
from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup, escape
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import __version__
from app.db.models import Audit, Finding, Metric, ModuleRun, Page, Site, SiteFacts
from app.modules.base import CATEGORY_LABELS
from app.modules.registry import all_modules

TEMPLATES = Path(__file__).parent / "templates"
STATUS_ORDER = {"FAIL": 0, "WARNING": 1, "NOT_CHECKED": 2, "PASS": 3, "N/A": 4}


_MODULE_NAMES: dict[str, str] = {}


def module_name(key: str) -> str:
    if not _MODULE_NAMES:
        _MODULE_NAMES.update({m.key: m.name for m in all_modules()})
    return _MODULE_NAMES.get(key, key)


def finding_dict(f: Finding) -> dict[str, Any]:
    return {
        "id": f.id, "module": f.module, "module_name": module_name(f.module),
        "module_number": f.module_number, "check_id": f.check_id,
        "category": f.category, "status": f.status, "title": f.title, "summary": f.summary,
        "why": f.why, "fix": f.fix, "reason": f.reason, "impact": f.impact, "effort": f.effort,
        "priority": f.priority, "score": f.score, "affected": f.affected, "total": f.total,
        "items": f.items, "items_truncated": f.items_truncated, "generated_fix": f.generated_fix,
        "sources": f.sources, "missing_label": f.missing_label,
    }


def missing_summary(findings: list[Finding]) -> list[str]:
    out = []
    for f in findings:
        if f.status in ("FAIL", "WARNING") and f.missing_label:
            out.append(f.missing_label.format(affected=f.affected, total=f.total or "?"))
    return out


def top_fixes(findings: list[Finding], n: int = 10) -> list[Finding]:
    issues = [f for f in findings if f.status in ("FAIL", "WARNING")]
    issues.sort(key=lambda f: (-f.priority, 0 if f.status == "FAIL" else 1, -f.affected, f.check_id))
    return issues[:n]


def build_report_data(db: Session, audit_id: int) -> dict[str, Any]:
    audit = db.get(Audit, audit_id)
    if audit is None:
        raise KeyError(audit_id)
    site = db.get(Site, audit.site_id)
    findings = db.execute(select(Finding).where(Finding.audit_id == audit_id)
                          .order_by(Finding.module_number, Finding.id)).scalars().all()
    runs = db.execute(select(ModuleRun).where(ModuleRun.audit_id == audit_id)).scalars().all()
    facts = db.get(SiteFacts, audit_id)
    metrics = db.execute(select(Metric).where(Metric.audit_id == audit_id)).scalars().all()
    pages = db.execute(select(Page.url, Page.status_code, Page.title, Page.indexable, Page.depth,
                              Page.inlinks, Page.word_count, Page.in_sitemap, Page.response_ms)
                       .where(Page.audit_id == audit_id).order_by(Page.depth.nulls_last(), Page.url)).all()
    f = facts.data if facts else {}
    by_cat: dict[str, list[dict]] = {k: [] for k in CATEGORY_LABELS}
    for x in findings:
        by_cat.setdefault(x.category, []).append(finding_dict(x))
    by_cat = {k: v for k, v in by_cat.items() if v}
    for lst in by_cat.values():
        lst.sort(key=lambda d: (d["module_number"], STATUS_ORDER.get(d["status"], 9), -d["priority"]))
    counts: dict[str, int] = {}
    for x in findings:
        counts[x.status] = counts.get(x.status, 0) + 1
    return {
        "generator": f"SEO Inspector {__version__}",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "audit": {"id": audit.id, "url": audit.input.get("url"), "domain": site.domain if site else None,
                  "status": audit.status, "created_at": audit.created_at.isoformat() if audit.created_at else None,
                  "finished_at": audit.finished_at.isoformat() if audit.finished_at else None,
                  "input": audit.input, "cost": audit.cost},
        "scores": {**(audit.scores or {}), "categories": {
            k: (audit.scores or {}).get("categories", {})[k] for k in CATEGORY_LABELS
            if k in (audit.scores or {}).get("categories", {})}},
        "coverage": audit.coverage, "status_counts": counts,
        "category_labels": CATEGORY_LABELS,
        "top_fixes": [finding_dict(x) for x in top_fixes(findings)],
        "missing": missing_summary(findings),
        "findings_by_category": by_cat,
        "not_checked": [finding_dict(x) for x in findings if x.status == "NOT_CHECKED"],
        "modules": [{"module": r.module, "status": r.status, "coverage": r.coverage, "error": r.error,
                     "duration_ms": r.duration_ms} for r in runs],
        "site": {k: f.get(k) for k in ("start_url", "origin", "host", "robots", "ai_bots", "sitemaps",
                                       "sitemap_found", "sitemap_url_count", "llms_txt", "https", "variants",
                                       "js_site", "crawl")},
        "metrics": [{"module": m.module, "key": m.key, "value": m.value, "source": m.source} for m in metrics],
        "pages": [dict(r._mapping) for r in pages],
    }


# ------------------------------------------------------------------ renderers


def _fmt(v: Any, depth: int = 0) -> str:
    if isinstance(v, dict):
        parts = [f"{k}: {_fmt(x, depth + 1)}" for k, x in v.items() if x not in (None, [], {}, "")]
        return "; ".join(parts) if depth == 0 else "{" + ", ".join(parts) + "}"
    if isinstance(v, list):
        shown = [_fmt(x, depth + 1) for x in v[:10]]
        more = f" … +{len(v) - 10}" if len(v) > 10 else ""
        return "[" + ", ".join(shown) + more + "]"
    return str(v)


def fmt_evidence(v: Any) -> Markup:
    return Markup(escape(_fmt(v)[:1500]))


def fix_code(fix: Any) -> str:
    if not fix:
        return ""
    if isinstance(fix, dict):
        for k in ("code", "content"):
            if fix.get(k):
                return str(fix[k])
        if fix.get("type") == "redirect_rule":
            return f"# Apache\n{fix.get('apache')}\n# Nginx\n{fix.get('nginx')}"
        return json.dumps(fix, indent=2, ensure_ascii=False)
    return str(fix)


def _env() -> Environment:
    env = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=select_autoescape(["html", "j2"]),
                      trim_blocks=True, lstrip_blocks=True)
    env.filters["evidence"] = fmt_evidence
    env.filters["fix_code"] = fix_code
    return env


def render_html(data: dict[str, Any]) -> str:
    return _env().get_template("report.html.j2").render(**data)


def render_json(data: dict[str, Any]) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False, default=str)


CSV_COLUMNS = ["audit_id", "category", "module", "check_id", "title", "status", "impact", "effort", "priority",
               "url", "evidence", "generated_fix", "why", "fix", "reason", "sources"]


def render_csv(data: dict[str, Any]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(CSV_COLUMNS)
    aid = data["audit"]["id"]
    for cat, lst in data["findings_by_category"].items():
        for f in lst:
            base = [aid, cat, f["module"], f["check_id"], f["title"], f["status"], f["impact"], f["effort"],
                    f["priority"]]
            tail = [f["why"], f["fix"], f["reason"], "; ".join(f["sources"] or [])]
            items = f["items"] if f["status"] in ("FAIL", "WARNING") and f["items"] else [None]
            for it in items:
                if it is None:
                    w.writerow(base + ["", f["summary"] or "", fix_code(f["generated_fix"])] + tail)
                else:
                    w.writerow(base + [it.get("url") or "", _fmt(it.get("evidence")),
                                       fix_code(it.get("generated_fix") or f["generated_fix"])] + tail)
    return buf.getvalue()
