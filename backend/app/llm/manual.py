"""Manual AI mode: the model's answers are supplied by hand instead of an API call.

Used when no LLM API key is available. Each AI request an audit makes is saved (prompt + JSON schema);
someone (e.g. Claude in a Claude Code session) writes the answers; they are validated against the
schema and stored in the LLM cache; re-running the audit then serves every answer from the cache.

    python -m app.llm.manual export DIR [--task keywords.map]   # write pending requests as JSON files
    python -m app.llm.manual import DIR                         # validate DIR/*.answer.json into the cache
    python -m app.llm.manual status
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app import cache
from app.db.models import CacheEntry
from app.db.session import session_scope
from app.llm.client import LLMError

MODEL_LABEL = "manual"
LABEL = "Claude (manual mode via Claude Code)"
PENDING = "llm_pending"
TTL = timedelta(days=30)


class AwaitingAnswer(LLMError):
    pass


def request_key(system: str, content: list, schema: dict, max_tokens: int) -> str:
    return cache.cache_key("llm-v1", MODEL_LABEL, "n/a", system, content, schema, max_tokens)


class ManualLLM:
    """Same interface as LLMClient.json(); answers come from the cache or are queued."""

    def __init__(self, audit_id: int | None):
        self.audit_id = audit_id
        self.queued = 0
        self.stats = type("S", (), {"usd": 0.0, "calls": 0, "cached": 0, "by_task": {}})()

    async def json(self, *, task: str, role: str, system: str, user: str | list, schema: dict,
                   max_tokens: int = 8000, use_cache: bool = True) -> Any:
        content = user if isinstance(user, list) else [{"type": "text", "text": user}]
        key = request_key(system, content, schema, max_tokens)
        hit = await asyncio.to_thread(cache.get, "llm", key)
        if hit is not None:
            self.stats.cached += 1
            return hit
        await asyncio.to_thread(cache.put, PENDING, key, {"task": task, "role": role, "system": system,
                                                          "content": content, "schema": schema,
                                                          "audit_id": self.audit_id}, TTL)
        self.queued += 1
        raise AwaitingAnswer("Awaiting an answer in manual AI mode (request saved; answer it and re-run the audit)")

    async def answer(self, **kw):
        raise LLMError("Needs live web search through an AI API; not available in manual AI mode")


# ---------------------------------------------------------------------------- validation
def validate(value: Any, schema: dict, path: str = "$") -> list[str]:
    """Minimal JSON-Schema check for the strict schemas this app uses."""
    errs: list[str] = []
    t = schema.get("type")
    if "enum" in schema and value not in schema["enum"]:
        errs.append(f"{path}: {value!r} not in {schema['enum']}")
    if t == "object":
        if not isinstance(value, dict):
            return [f"{path}: expected object"]
        for k in schema.get("required", []):
            if k not in value:
                errs.append(f"{path}.{k}: missing")
        if schema.get("additionalProperties") is False:
            errs += [f"{path}.{k}: unexpected" for k in value if k not in schema.get("properties", {})]
        for k, sub in schema.get("properties", {}).items():
            if k in value:
                errs += validate(value[k], sub, f"{path}.{k}")
    elif t == "array":
        if not isinstance(value, list):
            return [f"{path}: expected array"]
        for i, v in enumerate(value):
            errs += validate(v, schema.get("items", {}), f"{path}[{i}]")
    elif t == "string" and not isinstance(value, str):
        errs.append(f"{path}: expected string")
    elif t == "boolean" and not isinstance(value, bool):
        errs.append(f"{path}: expected boolean")
    elif t == "integer" and (not isinstance(value, int) or isinstance(value, bool)):
        errs.append(f"{path}: expected integer")
    elif t == "number" and (not isinstance(value, (int, float)) or isinstance(value, bool)):
        errs.append(f"{path}: expected number")
    return errs


# ---------------------------------------------------------------------------- CLI
def pending(task: str | None = None, audit_id: int | None = None) -> list[tuple[str, dict]]:
    with session_scope() as db:
        rows = db.execute(select(CacheEntry.key, CacheEntry.value).where(CacheEntry.namespace == PENDING)).all()
        answered = set(db.execute(select(CacheEntry.key).where(CacheEntry.namespace == "llm",
                                                               CacheEntry.key.in_([k for k, _ in rows]))).scalars())
    out = [(k, v) for k, v in rows if k not in answered and (not task or v["task"] == task)
           and (audit_id is None or v.get("audit_id") == audit_id)]
    return sorted(out, key=lambda kv: kv[1]["task"])


def export(directory: Path, task: str | None, audit_id: int | None) -> int:
    directory.mkdir(parents=True, exist_ok=True)
    items = pending(task, audit_id)
    for key, v in items:
        (directory / f"{key[:16]}.request.json").write_text(json.dumps({"key": key, **v}, indent=1, ensure_ascii=False))
    return len(items)


def import_answers(directory: Path) -> tuple[int, list[str]]:
    reqs = {json.loads(p.read_text())["key"][:16]: json.loads(p.read_text())
            for p in directory.glob("*.request.json")}
    ok, problems = 0, []
    for p in sorted(directory.glob("*.answer.json")):
        short = p.name.split(".")[0]
        req = reqs.get(short)
        if req is None:
            problems.append(f"{p.name}: no matching request file")
            continue
        try:
            value = json.loads(p.read_text())
        except json.JSONDecodeError as e:
            problems.append(f"{p.name}: invalid JSON ({e})")
            continue
        errs = validate(value, req["schema"])
        if errs:
            problems.append(f"{p.name}: " + "; ".join(errs[:5]))
            continue
        cache.put("llm", req["key"], value, TTL)
        ok += 1
    return ok, problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="app.llm.manual")
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("export"); e.add_argument("dir"); e.add_argument("--task"); e.add_argument("--audit", type=int)
    i = sub.add_parser("import"); i.add_argument("dir")
    sub.add_parser("status")
    a = ap.parse_args(argv)
    if a.cmd == "export":
        print(f"exported {export(Path(a.dir), a.task, a.audit)} request(s) to {a.dir}")
    elif a.cmd == "import":
        n, problems = import_answers(Path(a.dir))
        print(f"imported {n} answer(s)")
        for pr in problems:
            print("  rejected:", pr)
        return 1 if problems else 0
    else:
        from collections import Counter
        print(dict(Counter(v["task"] for _, v in pending())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
