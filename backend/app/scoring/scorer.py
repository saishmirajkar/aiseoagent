"""Scoring, coverage and prioritisation (formulas documented in README.md#scoring).

Check score
    PASS = 100. Site-level checks: WARNING = 50, FAIL = 0.
    Population checks (affected of total): score = 100 × (1 − s × min(1, √(affected/total)))
    with s = 1.0 for FAIL and 0.5 for WARNING. The square root makes the first few affected
    pages count noticeably without letting one bad page zero the check.
Category score
    Weighted mean of measured check scores in the category (weight = check impact: High 3,
    Med 2, Low 1). NOT_CHECKED and N/A checks are excluded.
Overall score
    Weighted mean of category scores using the configured category weights, re-normalised over
    categories that have at least one measured check.
Coverage
    Per module: measured check weight ÷ applicable check weight (planned modules = 0).
    Per category: mean of its modules. Overall: category-weighted mean over applicable categories.
Priority
    impact ÷ effort (High 3, Med 2, Low 1). Ties: FAIL before WARNING, then more affected URLs.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from app.modules.base import CheckModule, CheckResult, Level, Status

LEVEL_NUM = {Level.HIGH: 3, Level.MED: 2, Level.LOW: 1}


def check_score(r: CheckResult) -> float | None:
    if r.status == Status.PASS:
        return 100.0
    if r.status not in (Status.WARNING, Status.FAIL):
        return None
    s = 1.0 if r.status == Status.FAIL else 0.5
    if r.total:
        ratio = max(0.0, min(1.0, r.affected / r.total))
        return round(100.0 * (1 - s * min(1.0, math.sqrt(ratio))), 1)
    return 0.0 if r.status == Status.FAIL else 50.0


def priority(r: CheckResult) -> float:
    impact = r.impact or r.spec.impact
    return round(LEVEL_NUM[impact] / LEVEL_NUM[r.spec.effort], 3)


def priority_key(r: CheckResult) -> tuple:
    return (-priority(r), 0 if r.status == Status.FAIL else 1, -r.affected, r.spec.id)


@dataclass
class ScoreCard:
    overall: float | None
    categories: dict[str, dict] = field(default_factory=dict)
    coverage: float = 0.0
    modules: dict[str, dict] = field(default_factory=dict)


def compute(modules: list[CheckModule], results: dict[str, list[CheckResult]],
            category_weights: dict[str, float]) -> ScoreCard:
    cats: dict[str, dict] = {}
    mod_cov: dict[str, dict] = {}
    for m in modules:
        rs = results.get(m.key, [])
        cat = cats.setdefault(m.category.value, {"wsum": 0.0, "ssum": 0.0, "module_cov": [], "applicable": False,
                                                 "measured": 0, "not_checked": 0})
        applicable = [r for r in rs if r.status != Status.NOT_APPLICABLE]
        app_w = sum(r.spec.score_weight for r in applicable)
        meas = [r for r in applicable if r.status != Status.NOT_CHECKED]
        meas_w = sum(r.spec.score_weight for r in meas)
        cov = (meas_w / app_w) if app_w else None
        mod_cov[m.key] = {"coverage": None if cov is None else round(cov * 100, 1), "measured": len(meas),
                          "checks": len(rs), "category": m.category.value}
        if cov is not None:
            cat["module_cov"].append(cov)
            cat["applicable"] = True
        for r in meas:
            sc = check_score(r)
            if sc is None:
                continue
            w = r.spec.score_weight
            cat["wsum"] += w
            cat["ssum"] += w * sc
            cat["measured"] += 1
        cat["not_checked"] += sum(1 for r in applicable if r.status == Status.NOT_CHECKED)

    out_cats: dict[str, dict] = {}
    num = den = cov_num = cov_den = 0.0
    for key, c in cats.items():
        weight = float(category_weights.get(key, 0))
        score = round(c["ssum"] / c["wsum"], 1) if c["wsum"] else None
        cov = (sum(c["module_cov"]) / len(c["module_cov"])) if c["module_cov"] else 0.0
        out_cats[key] = {"score": score, "weight": weight, "coverage": round(cov * 100, 1),
                         "measured_checks": c["measured"], "not_checked": c["not_checked"],
                         "applicable": c["applicable"]}
        if score is not None and weight > 0:
            num += weight * score
            den += weight
        if c["applicable"] and weight > 0:
            cov_num += weight * cov
            cov_den += weight
    overall = round(num / den, 1) if den else None
    coverage = round(100 * cov_num / cov_den, 1) if cov_den else 0.0
    # effective weights after re-normalisation (shown in the report)
    for c in out_cats.values():
        c["effective_weight"] = round(100 * c["weight"] / den, 1) if c["score"] is not None and den else 0.0
    return ScoreCard(overall=overall, categories=out_cats, coverage=coverage, modules=mod_cov)
