"""Gemini on Vertex AI (optional second engine for the AI-visibility test).

Auth: a Google Cloud service account (JSON key in the GCP_SERVICE_ACCOUNT_JSON secret, role
"Vertex AI User"), plus GCP_PROJECT and GCP_LOCATION. Answers are grounded with Google Search so
they resemble what Gemini users see. Cost is estimated from the configured prices (Settings → AI)
and capped by the monthly Gemini budget.
"""
from __future__ import annotations

import asyncio
import json
import time

import httpx

from app import usage
from app.config import RuntimeSettings, env, ssl_context
from app.llm.client import Answer, BudgetExceeded, LLMError

SCOPE = "https://www.googleapis.com/auth/cloud-platform"


class GeminiClient:
    def __init__(self, settings: RuntimeSettings, audit_id: int | None):
        e = env()
        self.s = settings
        self.audit_id = audit_id
        self.project, self.location = e.gcp_project, e.gcp_location or "us-central1"
        self._info = json.loads(e.gcp_service_account_json or "{}")
        self._token: str | None = None
        self._expiry = 0.0
        self.usd = 0.0

    @property
    def model(self) -> str:
        return self.s.llm.gemini_model

    def _fetch_token(self) -> tuple[str, float]:
        from google.auth.transport.requests import Request
        from google.oauth2 import service_account
        creds = service_account.Credentials.from_service_account_info(self._info, scopes=[SCOPE])
        creds.refresh(Request())
        return creds.token, time.time() + 3000

    async def _auth(self) -> str:
        if not self._token or time.time() > self._expiry:
            try:
                self._token, self._expiry = await asyncio.to_thread(self._fetch_token)
            except Exception as ex:
                raise LLMError(f"Vertex AI authentication failed: {type(ex).__name__}") from ex
        return self._token

    def _cost(self, usage_md: dict, grounded: bool) -> float:
        l = self.s.llm
        return (usage_md.get("promptTokenCount", 0) * l.gemini_usd_per_m_input
                + usage_md.get("candidatesTokenCount", 0) * l.gemini_usd_per_m_output) / 1e6 + \
            (l.gemini_usd_per_grounded_request if grounded else 0)

    async def answer(self, *, task: str, prompt: str, grounded: bool = True) -> Answer:
        l = self.s.llm
        worst = (len(prompt) / 3 * l.gemini_usd_per_m_input + 2048 * l.gemini_usd_per_m_output) / 1e6 + \
            (l.gemini_usd_per_grounded_request if grounded else 0)
        month = await asyncio.to_thread(usage.used, "vertex_gemini", month=True, field="usd")
        if month + worst > self.s.budgets.gemini_usd_per_month:
            raise BudgetExceeded(f"Monthly Gemini budget (${self.s.budgets.gemini_usd_per_month:.2f}) would be exceeded")
        token = await self._auth()
        url = (f"https://{self.location}-aiplatform.googleapis.com/v1/projects/{self.project}/locations/"
               f"{self.location}/publishers/google/models/{self.model}:generateContent")
        body: dict = {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
                      "generationConfig": {"maxOutputTokens": 2048}}
        if grounded:
            body["tools"] = [{"googleSearch": {}}]
        try:
            async with httpx.AsyncClient(verify=ssl_context(), timeout=120) as c:
                r = await c.post(url, json=body, headers={"Authorization": f"Bearer {token}"})
        except httpx.HTTPError as ex:
            raise LLMError(f"Vertex AI unreachable: {type(ex).__name__}") from ex
        if r.status_code != 200:
            try:
                msg = r.json().get("error", {}).get("message", "")
            except ValueError:
                msg = r.text[:200]
            raise LLMError(f"Vertex AI HTTP {r.status_code}: {msg}"[:300])
        return await self._parse(task, r.json(), grounded)

    async def _parse(self, task: str, d: dict, grounded: bool) -> Answer:
        cost = self._cost(d.get("usageMetadata", {}), grounded)
        self.usd += cost
        await asyncio.to_thread(usage.record, "vertex_gemini", task, 1, cost, self.audit_id)
        cand = (d.get("candidates") or [{}])[0]
        if cand.get("finishReason") in ("SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST"):
            raise LLMError(f"Gemini declined ({cand['finishReason']})")
        text = "".join(p.get("text", "") for p in (cand.get("content") or {}).get("parts", []))
        cited = []
        for ch in (cand.get("groundingMetadata") or {}).get("groundingChunks", []) or []:
            web = ch.get("web") or {}
            # grounding URIs are redirect links; the title carries the source domain
            ref = web.get("title") or web.get("uri")
            if ref and ref not in cited:
                cited.append(ref if "://" in ref else f"https://{ref}")
        searches = len((cand.get("groundingMetadata") or {}).get("webSearchQueries", []) or [])
        return Answer(text=text.strip(), cited_urls=cited, searched=searches, model=self.model)


def make_gemini(settings: RuntimeSettings, audit_id: int | None) -> GeminiClient | None:
    e = env()
    if not (e.gcp_service_account_json and e.gcp_project):
        return None
    return GeminiClient(settings, audit_id)
