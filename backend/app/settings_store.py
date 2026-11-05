"""Runtime settings persisted in the ``settings`` table (key ``runtime``)."""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.config import RuntimeSettings
from app.db.models import SettingKV

KEY = "runtime"


def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load(db: Session) -> RuntimeSettings:
    row = db.get(SettingKV, KEY)
    defaults = RuntimeSettings().model_dump()
    if row is None:
        return RuntimeSettings()
    return RuntimeSettings.model_validate(_deep_merge(defaults, row.value))


def save(db: Session, patch: dict[str, Any]) -> RuntimeSettings:
    """Validate the merged settings, then persist. Raises pydantic.ValidationError."""
    current = load(db).model_dump()
    merged = RuntimeSettings.model_validate(_deep_merge(current, patch))
    row = db.get(SettingKV, KEY)
    if row is None:
        db.add(SettingKV(key=KEY, value=merged.model_dump()))
    else:
        row.value = merged.model_dump()
    db.flush()
    return merged
