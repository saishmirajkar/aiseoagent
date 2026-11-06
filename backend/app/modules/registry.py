"""Module registry + safe runner.

Planned modules are registered as placeholders so the report always lists all 17 factor
areas, and so coverage % honestly reflects what is not measured yet.
"""
from __future__ import annotations

import asyncio
import logging
import time
import traceback

from app.modules.base import (
    AuditContext, Category, CheckModule, CheckResult, CheckSpec, Level, Status,
)
from app.modules.m01_keywords import KeywordResearch
from app.modules.m02_content import ContentOptimization
from app.modules.m03_meta_tags import MetaTags
from app.modules.m04_internal_links import InternalLinking
from app.modules.m05_images import ImageSEO
from app.modules.m06_technical import TechnicalAudit
from app.modules.m07_indexing import CrawlingIndexing
from app.modules.m08_performance import Performance
from app.modules.m09_schema import SchemaMarkup
from app.modules.m17_geo import GeoContent

log = logging.getLogger(__name__)
MODULE_TIMEOUT_S = 900


class PlannedModule(CheckModule):
    def __init__(self, key: str, number: int, name: str, category: Category, phase: int):
        self.key, self.number, self.name, self.category = key, number, name, category
        self.planned_phase = phase
        self.checks = (CheckSpec(f"{key}.module", name, Level.MED, Level.MED,
                                 "Not built yet.", "Not built yet."),)

    async def run(self, ctx: AuditContext) -> list[CheckResult]:
        return [self.not_checked(f"{self.key}.module",
                                 f"This module is not built yet (planned for build phase {self.planned_phase})")]


def all_modules() -> list[CheckModule]:
    mods: list[CheckModule] = [
        KeywordResearch(),
        ContentOptimization(),
        MetaTags(),
        InternalLinking(),
        ImageSEO(),
        TechnicalAudit(),
        CrawlingIndexing(),
        Performance(),
        SchemaMarkup(),
        PlannedModule("backlinks", 10, "Backlink Analysis", Category.OFFPAGE, 4),
        PlannedModule("outreach", 11, "Link Building & Outreach", Category.OFFPAGE, 4),
        PlannedModule("brand_mentions", 12, "Brand Mentions & Reputation", Category.OFFPAGE, 4),
        PlannedModule("gbp", 13, "Google Business Profile", Category.LOCAL, 5),
        PlannedModule("citations", 14, "Citations & NAP", Category.LOCAL, 5),
        PlannedModule("reviews", 15, "Review Management", Category.LOCAL, 5),
        PlannedModule("ai_visibility", 16, "AI Search Visibility", Category.GEO, 4),
        GeoContent(),
    ]
    return order_modules(mods)


def order_modules(mods: list[CheckModule]) -> list[CheckModule]:
    """Topological order on depends_on, stable by module number."""
    by_key = {m.key: m for m in mods}
    done: list[CheckModule] = []
    visiting: set[str] = set()

    def visit(m: CheckModule) -> None:
        if m in done:
            return
        if m.key in visiting:
            raise ValueError(f"Circular module dependency at {m.key}")
        visiting.add(m.key)
        for dep in m.depends_on:
            if dep in by_key:
                visit(by_key[dep])
        visiting.discard(m.key)
        done.append(m)

    for m in sorted(mods, key=lambda x: x.number):
        visit(m)
    return done


async def run_module(module: CheckModule, ctx: AuditContext) -> tuple[list[CheckResult], str, str | None, int]:
    """Run one module; never raises. Returns (results, status, error, duration_ms)."""
    t0 = time.monotonic()
    error = None
    try:
        results = await asyncio.wait_for(module.run(ctx), timeout=MODULE_TIMEOUT_S)
        status = "planned" if module.planned_phase else "done"
    except Exception as e:  # graceful failure: mark every check NOT_CHECKED with the error
        error = f"{type(e).__name__}: {e}"
        log.error("Module %s failed: %s\n%s", module.key, error, traceback.format_exc())
        results = [module.not_checked(c.id, f"Module error: {error}"[:500]) for c in module.checks]
        status = "failed"
    # every declared check must report exactly once
    seen = {r.spec.id for r in results}
    for c in module.checks:
        if c.id not in seen:
            results.append(module.not_checked(c.id, "The module did not evaluate this check"))
    ctx.results[module.key] = results
    return results, status, error, int((time.monotonic() - t0) * 1000)


def is_measured(r: CheckResult) -> bool:
    return r.status in (Status.PASS, Status.WARNING, Status.FAIL)
