"""Google PageSpeed Insights API v5 client + response parsing.

The key is sent in the ``X-Goog-Api-Key`` header (never in the URL, so it cannot leak into
logs). Field data (CrUX, real Chrome users, p75 over 28 days) is used when Google has it;
otherwise Lighthouse lab data, and every value carries its source label.
"""
from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from dataclasses import field as dc_field
from typing import Any

import httpx

from app.config import ssl_context

ENDPOINT = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"
SOURCE_FIELD = "PageSpeed Insights (CrUX field data, p75)"
SOURCE_ORIGIN = "PageSpeed Insights (CrUX origin-level field data, p75)"
SOURCE_LAB = "PageSpeed Insights (Lighthouse lab data)"

# Core Web Vitals thresholds (good, poor): value <= good is good, > poor is poor.
THRESHOLDS = {"lcp_ms": (2500, 4000), "inp_ms": (200, 500), "cls": (0.1, 0.25),
              "tbt_ms": (200, 600), "fcp_ms": (1800, 3000)}
_FIELD_KEYS = {"LARGEST_CONTENTFUL_PAINT_MS": "lcp_ms", "INTERACTION_TO_NEXT_PAINT": "inp_ms",
               "CUMULATIVE_LAYOUT_SHIFT_SCORE": "cls", "FIRST_CONTENTFUL_PAINT_MS": "fcp_ms"}


def rate(metric: str, value: float | None) -> str | None:
    if value is None:
        return None
    good, poor = THRESHOLDS[metric]
    return "good" if value <= good else "poor" if value > poor else "needs improvement"


@dataclass
class PsiResult:
    url: str
    strategy: str
    error: str | None = None
    score: int | None = None  # Lighthouse performance score 0-100
    field: dict[str, float] = dc_field(default_factory=dict)  # URL-level CrUX
    origin_field: dict[str, float] = dc_field(default_factory=dict)
    lab: dict[str, float] = dc_field(default_factory=dict)
    opportunities: list[dict] = dc_field(default_factory=list)
    lighthouse_version: str | None = None
    final_url: str | None = None

    def to_json(self) -> dict[str, Any]:
        return self.__dict__.copy()

    @classmethod
    def from_json(cls, d: dict) -> PsiResult:
        return cls(**d)


def _field(block: dict | None) -> dict[str, float]:
    out: dict[str, float] = {}
    for k, v in ((block or {}).get("metrics") or {}).items():
        name = _FIELD_KEYS.get(k)
        if name and "percentile" in v:
            val = float(v["percentile"])
            out[name] = val / 100 if name == "cls" else val  # API reports CLS × 100
    return out


def parse(url: str, strategy: str, d: dict) -> PsiResult:
    r = PsiResult(url=url, strategy=strategy)
    lr = d.get("lighthouseResult") or {}
    if lr.get("runtimeError"):
        r.error = f"Lighthouse error: {lr['runtimeError'].get('code')}: {lr['runtimeError'].get('message', '')[:200]}"
        return r
    r.lighthouse_version = lr.get("lighthouseVersion")
    r.final_url = lr.get("finalDisplayedUrl") or lr.get("finalUrl")
    perf = (lr.get("categories") or {}).get("performance") or {}
    if perf.get("score") is not None:
        r.score = round(perf["score"] * 100)
    r.field = _field(d.get("loadingExperience"))
    r.origin_field = _field(d.get("originLoadingExperience"))
    audits = lr.get("audits") or {}
    for key, name in (("largest-contentful-paint", "lcp_ms"), ("cumulative-layout-shift", "cls"),
                      ("total-blocking-time", "tbt_ms"), ("first-contentful-paint", "fcp_ms"),
                      ("speed-index", "speed_index_ms")):
        v = (audits.get(key) or {}).get("numericValue")
        if v is not None:
            r.lab[name] = round(float(v), 3 if name == "cls" else 0)
    groups = {ref["id"]: ref.get("group") for ref in perf.get("auditRefs", [])}
    for aid, a in audits.items():
        score = a.get("score")
        if score is None or score >= 0.9 or groups.get(aid) not in ("insights", "diagnostics", None):
            continue
        det = a.get("details") or {}
        savings = a.get("metricSavings") or {}
        ms = det.get("overallSavingsMs") or max([v for v in savings.values() if isinstance(v, (int, float))] or [0])
        by = det.get("overallSavingsBytes") or 0
        if ms <= 0 and by <= 0:
            continue
        r.opportunities.append({"id": aid, "title": a.get("title"), "savings_ms": round(ms or 0),
                                "savings_kb": round(by / 1024) if by else 0,
                                "display": a.get("displayValue")})
    r.opportunities.sort(key=lambda o: (-o["savings_ms"], -o["savings_kb"]))
    return r


class PageSpeedClient:
    def __init__(self, api_key: str | None, timeout: float = 120.0, retries: int = 2):
        self.api_key = api_key
        self.timeout = timeout
        self.retries = retries

    async def run(self, url: str, strategy: str) -> PsiResult:
        params = {"url": url, "strategy": strategy, "category": "performance"}
        headers = {"X-Goog-Api-Key": self.api_key} if self.api_key else {}
        last = "unknown error"
        async with httpx.AsyncClient(timeout=self.timeout, verify=ssl_context()) as client:
            for attempt in range(self.retries + 1):
                try:
                    resp = await client.get(ENDPOINT, params=params, headers=headers)
                except httpx.TimeoutException:
                    last = "PageSpeed request timed out"
                except httpx.TransportError as e:
                    last = f"{type(e).__name__}"
                else:
                    if resp.status_code == 200:
                        return parse(url, strategy, resp.json())
                    try:
                        msg = resp.json().get("error", {}).get("message", "")
                    except ValueError:
                        msg = resp.text[:200]
                    last = f"HTTP {resp.status_code}: {msg[:300]}"
                    if resp.status_code not in (429, 500, 502, 503, 504):
                        break  # 400 = Lighthouse could not load the page, 403 = key problem
                await asyncio.sleep(2 ** attempt * 3 + random.random())
        return PsiResult(url=url, strategy=strategy, error=last)
