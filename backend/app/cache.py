"""Postgres-backed cache for external API results (PageSpeed, search, LLM, keyword data)."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.db.models import CacheEntry
from app.db.session import session_scope


def cache_key(*parts: Any) -> str:
    raw = json.dumps(parts, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def get(namespace: str, key: str) -> Any | None:
    with session_scope() as db:
        row = db.execute(select(CacheEntry).where(
            CacheEntry.namespace == namespace, CacheEntry.key == key,
            CacheEntry.expires_at > datetime.now(timezone.utc))).scalar_one_or_none()
        return row.value if row else None


def put(namespace: str, key: str, value: Any, ttl: timedelta) -> None:
    exp = datetime.now(timezone.utc) + ttl
    with session_scope() as db:
        stmt = insert(CacheEntry).values(namespace=namespace, key=key, value=value, expires_at=exp)
        db.execute(stmt.on_conflict_do_update(index_elements=[CacheEntry.namespace, CacheEntry.key],
                                              set_={"value": value, "expires_at": exp,
                                                    "created_at": datetime.now(timezone.utc)}))
