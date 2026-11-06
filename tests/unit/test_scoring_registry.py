import asyncio

from app.modules.base import (
    Category, CheckModule, CheckResult, CheckSpec, FindingItem, Level, Status,
)
from app.modules.registry import PlannedModule, all_modules, order_modules, run_module
from app.scoring.scorer import check_score, compute, priority, priority_key

from tests.unit.helpers import ctx, page, site

H, M, L = Level.HIGH, Level.MED, Level.LOW


def spec(cid, impact=M, effort=L, population="pages"):
    return CheckSpec(cid, cid, impact, effort, "why", "fix", population=population)


def res(cid, status, affected=0, total=None, impact=M, effort=L):
    return CheckResult(spec=spec(cid, impact, effort), status=status, affected=affected, total=total,
                       items=[FindingItem(f"u{i}", {}) for i in range(affected)])


def test_check_score_formula():
    assert check_score(res("a", Status.PASS)) == 100
    assert check_score(res("a", Status.FAIL)) == 0
    assert check_score(res("a", Status.WARNING)) == 50
    assert check_score(res("a", Status.FAIL, 1, 100)) == 90.0      # 1 - sqrt(0.01)
    assert check_score(res("a", Status.FAIL, 25, 100)) == 50.0     # 1 - sqrt(0.25)
    assert check_score(res("a", Status.WARNING, 25, 100)) == 75.0  # 1 - 0.5*sqrt(0.25)
    assert check_score(res("a", Status.FAIL, 100, 100)) == 0.0
    assert check_score(res("a", Status.NOT_CHECKED)) is None


def test_priority_order():
    a = res("a", Status.WARNING, 5, 10, impact=H, effort=L)  # 3.0
    b = res("b", Status.FAIL, 1, 10, impact=H, effort=M)     # 1.5
    c = res("c", Status.FAIL, 9, 10, impact=H, effort=L)     # 3.0, FAIL beats WARNING on ties
    assert priority(a) == 3.0 and priority(b) == 1.5
    assert [r.spec.id for r in sorted([a, b, c], key=priority_key)] == ["c", "a", "b"]


class _Mod(CheckModule):
    def __init__(self, key, cat, checks):
        self.key, self.number, self.name, self.category = key, 1, key, cat
        self.checks = tuple(checks)


def test_compute_excludes_not_checked_and_reweights():
    tech = _Mod("t", Category.TECHNICAL, [spec("t1", H), spec("t2", L)])
    onp = _Mod("o", Category.ONPAGE, [spec("o1"), spec("o2")])
    perf = PlannedModule("performance", 8, "Perf", Category.PERFORMANCE, 2)
    results = {
        "t": [res("t1", Status.PASS, impact=H), res("t2", Status.FAIL, impact=L)],
        "o": [res("o1", Status.PASS), res("o2", Status.NOT_CHECKED)],
        "performance": [CheckResult(spec=perf.checks[0], status=Status.NOT_CHECKED)],
    }
    card = compute([tech, onp, perf], results, {"technical": 25, "onpage": 30, "performance": 15})
    assert card.categories["technical"]["score"] == 75.0  # (3*100 + 1*0) / 4
    assert card.categories["onpage"]["score"] == 100.0    # NOT_CHECKED excluded
    assert card.categories["performance"]["score"] is None
    assert card.overall == round((25 * 75 + 30 * 100) / 55, 1)  # performance re-weighted out
    # coverage: technical 100%, onpage 50%, performance 0% -> (25*1 + 30*.5 + 15*0)/70
    assert card.coverage == round(100 * (25 + 15) / 70, 1)


def test_not_applicable_is_excluded_from_coverage():
    m = _Mod("t", Category.TECHNICAL, [spec("t1"), spec("t2")])
    card = compute([m], {"t": [res("t1", Status.PASS), res("t2", Status.NOT_APPLICABLE)]}, {"technical": 25})
    assert card.coverage == 100.0


def test_module_failure_is_graceful():
    class Boom(CheckModule):
        key, number, name, category = "boom", 99, "Boom", Category.TECHNICAL
        checks = (spec("boom.a"), spec("boom.b"))

        async def run(self, c):
            raise RuntimeError("kaput")

    c = ctx(site([page("/")]))
    results, status, error, _ = asyncio.run(run_module(Boom(), c))
    assert status == "failed" and "kaput" in error
    assert {r.status for r in results} == {Status.NOT_CHECKED}
    assert all("kaput" in r.reason for r in results)


def test_missing_check_results_are_filled():
    class Lazy(CheckModule):
        key, number, name, category = "lazy", 98, "Lazy", Category.TECHNICAL
        checks = (spec("lazy.a"), spec("lazy.b"))

        async def run(self, c):
            return [self.result("lazy.a", Status.PASS)]

    results, status, _, _ = asyncio.run(run_module(Lazy(), ctx(site([page("/")]))))
    assert status == "done"
    assert {r.spec.id: r.status for r in results}["lazy.b"] == Status.NOT_CHECKED


def test_registry_has_17_modules_in_order():
    mods = all_modules()
    assert sorted(m.number for m in mods) == list(range(1, 18))
    pos = {m.key: i for i, m in enumerate(mods)}
    for m in mods:  # every module runs after the modules it depends on
        assert all(pos[d] < pos[m.key] for d in m.depends_on), m.key
    assert len({m.key for m in mods}) == 17
    for m in mods:
        assert len({c.id for c in m.checks}) == len(m.checks), m.key


def test_order_modules_respects_dependencies():
    a = _Mod("a", Category.ONPAGE, [])
    a.number = 1
    a.depends_on = ("b",)
    b = _Mod("b", Category.ONPAGE, [])
    b.number = 2
    assert [m.key for m in order_modules([a, b])] == ["b", "a"]
