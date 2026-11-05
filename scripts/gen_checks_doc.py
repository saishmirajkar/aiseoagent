"""Regenerate docs/CHECKS.md from the module registry:  PYTHONPATH=backend python scripts/gen_checks_doc.py"""
from pathlib import Path

from app.modules.base import CATEGORY_LABELS
from app.modules.registry import all_modules

lines = ["# Check reference", "", "Generated from the module registry (`scripts/gen_checks_doc.py`). "
         "Statuses: PASS, WARNING, FAIL, NOT_CHECKED (with reason), N/A (not applicable to this site).", ""]
for m in all_modules():
    lines.append(f"## {m.number}. {m.name} ({CATEGORY_LABELS[m.category.value]})")
    lines.append("")
    if m.planned_phase:
        lines.append(f"_Not built yet; planned for build phase {m.planned_phase}. Reported as NOT_CHECKED._")
        lines.append("")
        continue
    lines.append("| Check ID | Check | Impact | Effort | Fix |")
    lines.append("|---|---|---|---|---|")
    for c in m.checks:
        lines.append(f"| `{c.id}` | {c.title} | {c.impact.value} | {c.effort.value} | {c.fix.replace('|', '/')} |")
    lines.append("")
Path(__file__).resolve().parents[1].joinpath("docs/CHECKS.md").write_text("\n".join(lines))
print("wrote docs/CHECKS.md")
