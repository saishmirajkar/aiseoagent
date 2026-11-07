"""Module 11: Link Building & Outreach — prospects, public contact info, email drafts, tracker.

NEVER sends anything: drafts are stored in the outreach tracker for the user to review and send.
Contact details are only taken from emails publicly listed on the prospect's own pages.
"""
from __future__ import annotations

import asyncio
import re
from collections import Counter
from urllib.parse import urlsplit

from sqlalchemy import select

from app.config import env
from app.crawler.fetcher import Fetcher
from app.crawler.rate_limit import HostRateLimiter
from app.crawler.robots import build_robots
from app.crawler.urls import host_of
from app.datalayer import authority
from app.datalayer.backlinks import SOCIAL_HOSTS, registered
from app.db.models import OutreachProspect
from app.db.session import session_scope
from app.llm.client import LLMError, STR, arr, INT, obj
from app.llm.manual import AwaitingAnswer
from app.llm.summaries import chunks
from app.modules.base import (
    AuditContext, Category, CheckModule, CheckResult, CheckSpec, FindingItem, Integration, Level, Need, Status,
)
from app.modules.m01_keywords import kw_tokens
from app.modules.m10_backlinks import NO_SEARCH, brand_of
from app.providers.search import SearchError

H, M, L = Level.HIGH, Level.MED, Level.LOW
EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,24}\b")
SKIP_HOSTS = SOCIAL_HOSTS + ("wikipedia.org", "amazon.", "flipkart.", "ebay.", "google.", "youtube.", "indiamart.",
                             "justdial.", "alibaba.", "medium.com", "github.com", "jotform.com")
DRAFT_SYSTEM = """You draft short, personalised outreach emails for a website's link building. Never claim
anything about the sender's business that is not in the provided site description. Keep each email under
140 words, friendly and specific to the prospect's page; propose one concrete thing (adding a link to an
existing mention, a guest article with two topic ideas, or a resource the prospect's readers would value).
No spammy phrases, no fake urgency. Return one draft per prospect id."""
DRAFT_SCHEMA = obj({"drafts": arr(obj({"id": INT, "subject": STR, "body": STR}))})


def public_emails(html_text: str, mailtos: list[str], domain: str) -> list[str]:
    found = []
    for e in mailtos + EMAIL.findall(html_text):
        e = e.strip().lower().removeprefix("mailto:").split("?")[0]
        if EMAIL.fullmatch(e) and not e.endswith((".png", ".jpg", ".webp")) and e not in found:
            found.append(e)
    same = [e for e in found if e.split("@")[1].endswith(domain)]
    return (same or found)[:3]


class LinkBuilding(CheckModule):
    key = "outreach"
    number = 11
    name = "Link Building & Outreach"
    category = Category.OFFPAGE
    needs = frozenset({Need.SITE_FACTS})
    integrations = frozenset({Integration.SEARCH, Integration.LLM})
    depends_on = ("backlinks", "brand_mentions")
    checks = (
        CheckSpec("out.prospects", "Outreach prospects", M, H,
                  "Relevant sites, blogs and publishers likely to link to you, ranked by relevance and authority.",
                  "Work through the list in the Outreach tracker (highest score first)."),
        CheckSpec("out.drafts", "Personalised outreach email drafts", L, M,
                  "Ready-to-edit emails save time; they are never sent automatically.",
                  "Review, personalise further and send from your own mailbox; update the tracker status."),
    )

    async def run(self, ctx: AuditContext) -> list[CheckResult]:
        if ctx.search is None and not ctx.shared.get("link_gap"):
            return [self.not_checked(c.id, NO_SEARCH) for c in self.checks]
        site_domain = registered(ctx.site.facts.host)
        comp_domains = {registered(host_of(c)) for c in ctx.inputs.competitors}
        kmap = ctx.shared.get("keyword_map", {})
        brand_words = kw_tokens(brand_of(ctx) or "") | kw_tokens(site_domain.split(".")[0])
        clusters = Counter(r["cluster"] for r in kmap.values()
                           if r.get("cluster") and not (kw_tokens(r["cluster"]) & brand_words))
        topic = (ctx.inputs.business_type or (ctx.inputs.keywords[0] if ctx.inputs.keywords else None)
                 or (clusters.most_common(1)[0][0] if clusters else None)
                 or brand_of(ctx) or site_domain.split(".")[0])
        topic_words = kw_tokens(topic) | {w for r in list(kmap.values())[:10] for w in kw_tokens(r.get("primary_keyword", ""))}
        cands: dict[str, dict] = {}

        def add(url: str, title: str, snippet: str, kind: str) -> None:
            dom = registered(host_of(url))
            if not dom or dom == site_domain or dom in comp_domains or any(s in dom for s in SKIP_HOSTS):
                return
            if dom in cands:
                return
            rel_words = kw_tokens(f"{title} {snippet}")
            relevance = len(rel_words & topic_words) / max(1, min(len(topic_words), 6))
            if kind in ("guest_post", "resource") and not relevance:
                return  # search results that share no topic words with the site are noise (e.g. generic news blogs)
            cands[dom] = {"url": url, "title": title, "snippet": snippet, "kind": kind, "domain": dom,
                          "relevance": round(min(1.0, relevance), 2)}

        disc = ctx.shared.get("discovery")
        for m in (disc.mentions if disc else []):
            add(m.source_url, m.title, m.snippet, "unlinked_mention")
        for g in ctx.shared.get("link_gap", [])[:20]:
            add(f"https://{g['domain']}/", g["domain"], "links to " + ", ".join(g["links_to"]), "link_gap")
        errors = []
        if ctx.search is not None:
            for q, kind in ((f'{topic} "write for us"', "guest_post"), (f'{topic} "guest post"', "guest_post"),
                            (f"{topic} blog", "resource")):
                try:
                    resp = await ctx.search.search(q, count=20, country=ctx.inputs.country)
                except SearchError as e:
                    errors.append(str(e))
                    break
                for r in resp.results:
                    add(r.url, r.title, r.snippet, kind)
        if not cands:
            reason = errors[0] if errors else "No prospects found"
            return [self.not_checked("out.prospects", reason), self.not_checked("out.drafts", reason)]
        auth = await asyncio.to_thread(authority.lookup, list(cands))
        for d, c in cands.items():
            a = (auth.get(d) or {}).get("score")
            c["authority"] = a
            bonus = {"unlinked_mention": 0.3, "link_gap": 0.2}.get(c["kind"], 0.0)
            c["score"] = round(0.5 * c["relevance"] + 0.3 * ((a or 5) / 100) + bonus, 3)
        top = sorted(cands.values(), key=lambda c: -c["score"])[:ctx.settings.data.outreach_prospects]
        await self._contacts(ctx, top)
        drafts_result = await self._drafts(ctx, top)
        await asyncio.to_thread(self._save, site_domain, top, ctx.audit_id)
        items = [FindingItem(p["url"], {"domain": p["domain"], "type": p["kind"], "relevance": p["relevance"],
                                        "authority": p["authority"] if p["authority"] is not None else "low/unknown",
                                        "public_contact": ", ".join(p.get("emails", [])) or p.get("contact_page") or "none listed"})
                 for p in top]
        out = [CheckResult(spec=self.spec("out.prospects"), status=Status.PASS,
                           summary=f"{len(top)} prospect(s) saved to the Outreach tracker (from {len(cands)} candidates)"
                                   + (f"; search stopped: {errors[0]}" if errors else ""),
                           items=items, total=len(cands),
                           sources=["Brave Search", "Common Crawl (authority)", "Public pages of each prospect"])]
        out.append(drafts_result)
        return out

    async def _contacts(self, ctx: AuditContext, prospects: list[dict]) -> None:
        cfg = ctx.settings.crawl
        sem = asyncio.Semaphore(4)
        async with Fetcher(env().user_agent, HostRateLimiter(cfg.rate_limit_per_sec), timeout=cfg.timeout_s,
                           retries=0, max_bytes=2_000_000) as f:
            async def one(p: dict) -> None:
                async with sem:
                    try:
                        parts = urlsplit(p["url"])
                        origin = f"{parts.scheme}://{parts.netloc}"
                        rr = await f.fetch(origin + "/robots.txt", max_bytes=200_000, retries=0)
                        robots = build_robots(origin + "/robots.txt", rr.status, rr.text() if rr.status == 200 else "", rr.error)
                        for u in (p["url"], origin + "/contact", origin + "/contact-us"):
                            if not robots.can_fetch(u, cfg.user_agent_token):
                                continue
                            res = await f.fetch(u, html_only=True, retries=0)
                            if res.status != 200 or not res.body:
                                continue
                            text = res.text()
                            mailtos = re.findall(r'href=["\']mailto:([^"\'?]+)', text, re.I)
                            emails = public_emails(text, mailtos, p["domain"])
                            if emails:
                                p["emails"] = emails
                                p["contact_page"] = u
                                return
                            if "contact" in u:
                                p["contact_page"] = u
                    except Exception:
                        return
            await asyncio.gather(*(one(p) for p in prospects))

    async def _drafts(self, ctx: AuditContext, prospects: list[dict]) -> CheckResult:
        if ctx.llm is None:
            return self.not_checked("out.drafts", "Anthropic API key not configured (add the ANTHROPIC_API_KEY secret)")
        home = ctx.site.homepage
        about = (f"Site: {ctx.site.facts.origin}\nName: {brand_of(ctx) or ''}\n"
                 f"Description: {(home.meta_description if home else '') or ''}\n"
                 f"Main topics: {', '.join(sorted({r['primary_keyword'] for r in ctx.shared.get('keyword_map', {}).values() if r.get('primary_keyword')})[:8])}")
        made, errors = 0, []
        for group in chunks(list(enumerate(prospects)), 5):
            lines = [f"{i}. {p['kind']} | {p['domain']} | page: {p['title']} | {p['snippet'][:250]}" for i, p in group]
            try:
                data = await ctx.llm.json(task="outreach.drafts", role="bulk", system=DRAFT_SYSTEM,
                                          user=f"<data>\nSENDER\n{about}\n\nPROSPECTS\n" + "\n".join(lines) + "\n</data>",
                                          schema=DRAFT_SCHEMA, max_tokens=4000)
            except LLMError as e:
                errors.append(str(e))
                if isinstance(e, AwaitingAnswer):
                    continue  # manual AI mode: queue every batch in one run
                break
            for d in data.get("drafts", []):
                if 0 <= d.get("id", -1) < len(prospects):
                    prospects[d["id"]]["draft"] = f"Subject: {d['subject']}\n\n{d['body']}"
                    made += 1
        if not made:
            return self.not_checked("out.drafts", f"LLM failed: {errors[0]}" if errors else "No drafts produced")
        items = [FindingItem(p["url"], {"domain": p["domain"]}, generated_fix={"type": "email_draft", "code": p["draft"]})
                 for p in prospects if p.get("draft")]
        return CheckResult(spec=self.spec("out.drafts"), status=Status.PASS,
                           summary=f"{made} email draft(s) saved to the Outreach tracker — review before sending; "
                                   "nothing is sent automatically", items=items,
                           sources=[f"AI drafts ({ctx.settings.llm.bulk_model})"])

    @staticmethod
    def _save(site_domain: str, prospects: list[dict], audit_id: int | None) -> None:
        with session_scope() as db:
            for p in prospects:
                row = db.execute(select(OutreachProspect).where(OutreachProspect.site_domain == site_domain,
                                                                OutreachProspect.url == p["url"])).scalar_one_or_none()
                contact = {k: v for k, v in (("emails", p.get("emails")), ("contact_page", p.get("contact_page"))) if v}
                if row is None:
                    db.add(OutreachProspect(site_domain=site_domain, prospect_domain=p["domain"], url=p["url"],
                                            title=p["title"][:500], kind=p["kind"], authority=p["authority"],
                                            relevance=p["relevance"], contact=contact, draft_email=p.get("draft"),
                                            audit_id=audit_id))
                else:  # refresh data, keep the user's status and notes
                    row.authority, row.relevance, row.audit_id = p["authority"], p["relevance"], audit_id
                    row.contact = contact or row.contact
                    row.draft_email = p.get("draft") or row.draft_email
