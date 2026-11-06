"""Usage ledger + hard budget caps for paid/quota'd APIs."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select

from app.db.models import UsageLedger
from app.db.session import session_scope


class BudgetExceeded(Exception):
    pass


def record(provider: str, kind: str, units: float = 1, cost_usd: float = 0.0, audit_id: int | None = None) -> None:
    with session_scope() as db:
        db.add(UsageLedger(provider=provider, kind=kind, units=units, cost_usd=cost_usd, audit_id=audit_id))


def _month_start() -> datetime:
    now = datetime.now(timezone.utc)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def used(provider: str, *, audit_id: int | None = None, month: bool = False, field: str = "units") -> float:
    col = UsageLedger.units if field == "units" else UsageLedger.cost_usd
    with session_scope() as db:
        q = select(func.coalesce(func.sum(col), 0)).where(UsageLedger.provider == provider)
        if audit_id is not None:
            q = q.where(UsageLedger.audit_id == audit_id)
        if month:
            q = q.where(UsageLedger.created_at >= _month_start())
        return float(db.execute(q).scalar() or 0)


def audit_cost(audit_id: int) -> dict[str, float]:
    with session_scope() as db:
        rows = db.execute(select(UsageLedger.provider, func.sum(UsageLedger.units), func.sum(UsageLedger.cost_usd))
                          .where(UsageLedger.audit_id == audit_id).group_by(UsageLedger.provider)).all()
    return {p: {"calls": float(u or 0), "usd": round(float(c or 0), 4)} for p, u, c in rows}
