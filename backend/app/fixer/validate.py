"""Checks run on every changed file before it can be proposed. A file that fails is dropped from the change set
and the reason is shown; we never propose a change that breaks the page structure or the structured data."""
from __future__ import annotations

import json
import re

from selectolax.lexbor import LexborHTMLParser

from app.modules.schema_rules import is_valid_jsonld

_LD = re.compile(r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', re.I | re.S)
_STRUCT_TAGS = ("html", "head", "body", "main", "header", "footer", "nav", "script", "style", "template")
BRACKETS = {"{": "}", "(": ")", "[": "]"}


def _tag_counts(text: str) -> dict[str, int]:
    out = {}
    for t in _STRUCT_TAGS:
        out[f"<{t}>"] = len(re.findall(rf"<{t}\b", text, re.I))
        out[f"</{t}>"] = len(re.findall(rf"</{t}\s*>", text, re.I))
    return out


def _balance(text: str) -> dict[str, int]:
    stripped = re.sub(r"(\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|`(?:\\.|[^`\\])*`)", "", text)
    return {o: stripped.count(o) - stripped.count(c) for o, c in BRACKETS.items()}


def check_file(path: str, before: str | None, after: str) -> list[str]:
    """Problems found in ``after`` (empty list = fine)."""
    problems: list[str] = []
    ext = path.rsplit(".", 1)[-1].lower()
    if ext in ("html", "htm", "liquid", "njk", "hbs", "ejs", "erb", "php", "twig", "astro"):
        if before is not None:
            b, a = _tag_counts(before), _tag_counts(after)
            for k in b:
                # added JSON-LD blocks legitimately add <script> tags; everything else must stay balanced
                if "script" not in k and a[k] != b[k]:
                    problems.append(f"number of {k} tags changed ({b[k]} → {a[k]})")
        try:
            LexborHTMLParser(after)
        except Exception as e:  # pragma: no cover - lexbor is very lenient
            problems.append(f"HTML could not be parsed: {e}")
    if ext in ("jsx", "tsx", "js", "ts", "mjs", "vue", "svelte") and before is not None:
        if _balance(after) != _balance(before):
            problems.append("brackets no longer balance (possible syntax error)")
    new_blocks = set(_LD.findall(after)) - set(_LD.findall(before or ""))
    for block in new_blocks:
        if "{" in block and "{{" not in block:
            try:
                data = json.loads(block)
            except ValueError as e:
                problems.append(f"structured data is not valid JSON: {e}")
                continue
            for obj in data if isinstance(data, list) else [data]:
                if isinstance(obj, dict):
                    ok, why = is_valid_jsonld(obj)
                    if not ok:
                        problems.append("structured data: " + "; ".join(why[:3]))
    if ext == "xml":
        try:
            from lxml import etree
            etree.fromstring(after.encode())
        except Exception as e:
            problems.append(f"XML is not valid: {e}")
    return problems
