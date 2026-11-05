"""Check-module contract and the shared finding schema.

A module declares its checks up front (``CheckSpec``) and returns exactly one
``CheckResult`` per check. Adding a factor = adding one module file + registering it.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any

from app.config import RuntimeSettings
from app.sitemodel.types import SiteModel


class Status(str, enum.Enum):
    PASS = "PASS"
    WARNING = "WARNING"
    FAIL = "FAIL"
    NOT_CHECKED = "NOT_CHECKED"
    NOT_APPLICABLE = "N/A"


class Level(str, enum.Enum):
    HIGH = "High"
    MED = "Med"
    LOW = "Low"

    @property
    def value_num(self) -> int:
        return {"High": 3, "Med": 2, "Low": 1}[self.value]


class Category(str, enum.Enum):
    TECHNICAL = "technical"
    ONPAGE = "onpage"
    PERFORMANCE = "performance"
    SCHEMA = "schema"
    OFFPAGE = "offpage"
    LOCAL = "local"
    GEO = "geo"


CATEGORY_LABELS = {
    "technical": "Technical", "onpage": "On-page", "performance": "Performance",
    "schema": "Schema", "offpage": "Off-page", "local": "Local", "geo": "AI Search & GEO",
}


class Need(str, enum.Enum):
    PAGES = "pages"
    LINKS = "links"
    IMAGES = "images"
    SITE_FACTS = "site_facts"
    SITEMAP = "sitemap"
    ROBOTS = "robots"


class Integration(str, enum.Enum):
    LLM = "anthropic"
    VISION = "anthropic_vision"
    SEARCH = "brave_search"
    PAGESPEED = "pagespeed"
    SEARCH_CONSOLE = "search_console"
    GOOGLE_ADS = "google_ads_keyword_planner"
    GBP = "google_business_profile"


@dataclass(frozen=True)
class CheckSpec:
    id: str
    title: str
    impact: Level
    effort: Level
    why: str
    fix: str
    population: str | None = None  # "pages" | "images" | "links" ... ; None = site-level
    missing_label: str | None = None  # e.g. "Meta descriptions missing on {affected} page(s)"
    weight: float | None = None  # defaults to impact value

    @property
    def score_weight(self) -> float:
        return self.weight if self.weight is not None else float(self.impact.value_num)


@dataclass
class FindingItem:
    url: str | None
    evidence: Any
    generated_fix: Any | None = None
    also_on: list[str] = field(default_factory=list)  # other pages with the same issue (e.g. a shared image)

    def as_dict(self) -> dict:
        d = {"url": self.url, "evidence": self.evidence}
        if self.generated_fix is not None:
            d["generated_fix"] = self.generated_fix
        if self.also_on:
            d["also_on"] = self.also_on[:500]
        return d


@dataclass
class CheckResult:
    spec: CheckSpec
    status: Status
    summary: str = ""
    items: list[FindingItem] = field(default_factory=list)
    affected: int = 0
    total: int | None = None
    reason: str | None = None  # NOT_CHECKED / N/A explanation
    generated_fix: Any | None = None  # site-level generated fix
    sources: list[str] = field(default_factory=lambda: ["Crawl"])
    impact: Level | None = None  # override spec (e.g. escalate severity)


@dataclass
class AuditInput:
    url: str
    business_type: str | None = None
    country: str | None = None
    language: str | None = None
    keywords: list[str] = field(default_factory=list)
    competitors: list[str] = field(default_factory=list)
    business_name: str | None = None
    address: str | None = None
    phone: str | None = None
    brand: str | None = None


@dataclass
class AuditContext:
    audit_id: int | None
    site: SiteModel
    settings: RuntimeSettings
    inputs: AuditInput
    integrations: dict[str, bool]
    results: dict[str, list[CheckResult]] = field(default_factory=dict)
    shared: dict[str, Any] = field(default_factory=dict)  # module outputs for dependents


class CheckModule:
    key: str = ""
    number: int = 0
    name: str = ""
    category: Category = Category.TECHNICAL
    needs: frozenset[Need] = frozenset()
    integrations: frozenset[Integration] = frozenset()
    depends_on: tuple[str, ...] = ()
    checks: tuple[CheckSpec, ...] = ()
    planned_phase: int | None = None  # set on placeholder modules not built yet

    def spec(self, check_id: str) -> CheckSpec:
        for c in self.checks:
            if c.id == check_id:
                return c
        raise KeyError(check_id)

    async def run(self, ctx: AuditContext) -> list[CheckResult]:
        raise NotImplementedError

    # ------------------------------------------------------------ helpers
    def result(self, check_id: str, status: Status, summary: str = "", **kw) -> CheckResult:
        return CheckResult(spec=self.spec(check_id), status=status, summary=summary, **kw)

    def not_checked(self, check_id: str, reason: str) -> CheckResult:
        return CheckResult(spec=self.spec(check_id), status=Status.NOT_CHECKED, reason=reason, sources=[])

    def not_applicable(self, check_id: str, reason: str) -> CheckResult:
        return CheckResult(spec=self.spec(check_id), status=Status.NOT_APPLICABLE, reason=reason, sources=[])

    def by_population(self, check_id: str, items: list[FindingItem], total: int, *,
                      fail: bool = True, summary: str = "", pass_summary: str = "",
                      **kw) -> CheckResult:
        """PASS when no items, else FAIL/WARNING with affected/total."""
        if total == 0:
            return self.not_applicable(check_id, "Nothing to evaluate on this site")
        if not items:
            return self.result(check_id, Status.PASS, pass_summary or "No issues found", total=total)
        affected = len({i.url for i in items}) if self.spec(check_id).population == "pages" else len(items)
        return self.result(check_id, Status.FAIL if fail else Status.WARNING, summary,
                           items=items, affected=affected, total=total, **kw)
