"""Prepare a fix run (read the repo, apply the chosen fixes, validate, diff) and publish it as a pull request."""
from __future__ import annotations

import difflib
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from app.fixer import html_edit as H
from app.fixer.detect import Framework
from app.fixer.llm_edit import ai_edit
from app.fixer.locate import Locator, PageInfo
from app.fixer.plan import FixItem, _path
from app.fixer.repo import Commit, Repo
from app.fixer.validate import check_file
from app.fixer.writing import Voice, faq_html, faq_jsonld, paragraphs_html, write
from app.llm.client import LLMError
from app.llm.manual import AwaitingAnswer
from app.reports.plain import AGENTS

log = logging.getLogger(__name__)
AGENT_ORDER = [a["name"] for a in AGENTS.values()]
SITEWIDE_ACTIONS = {"sitemap", "robots_sitemap", "llms_txt", "img_attrs"}


@dataclass
class Outcome:
    status: str = "done"  # done | skipped | waiting | failed
    reason: str = ""
    paths: list[str] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    text: str | None = None  # final text written for review items


class Skip(Exception):
    pass


class Prepared:
    """Working copy of changed files plus per-agent snapshots for commits."""

    def __init__(self, repo: Repo):
        self.repo = repo
        self.original: dict[str, str | None] = {}
        self.work: dict[str, str] = {}

    async def get(self, path: str) -> str | None:
        if path in self.work:
            return self.work[path]
        text = await self.repo.read(path) if self.repo.exists(path) else None
        self.original.setdefault(path, text)
        return text

    def put(self, path: str, text: str) -> None:
        if path not in self.original:
            self.original[path] = None  # new file
        self.work[path] = text


class FixRunner:
    def __init__(self, *, repo: Repo, fw: Framework, llm, pages: list[dict], texts: dict[str, str],
                 audit_input: dict, answers: dict[str, str], origin: str):
        self.repo, self.fw, self.llm = repo, fw, llm
        self.pages = {p["url"]: p for p in pages}
        self.page_list = pages
        self.texts = texts
        self.input = audit_input
        self.answers = {k: (v or "").strip() for k, v in answers.items()}
        self.origin = origin.rstrip("/")
        self.prep = Prepared(repo)
        self.locator = Locator(repo, fw)
        self._locs: dict[str, list] = {}
        self.new_titles: dict[str, str] = {}  # titles changed in this run (used by llms.txt)
        from app.fixer.writing import voice_profile
        self.voice: Voice = voice_profile(list(texts.values())[:8], audit_input.get("brand"))

    # ------------------------------------------------------------------ helpers
    async def _page_file(self, url: str, *, allow_shared: bool = False) -> str:
        if url not in self._locs:
            p = self.pages.get(url, {})
            h1 = next((t for lvl, t in (p.get("data") or {}).get("headings", []) if lvl == 1 and t), None)
            self._locs[url] = await self.locator.locate(PageInfo(url, p.get("title"), p.get("meta_description"), h1))
            self.locator.mark_shared(self._locs[url])
        locs = self._locs[url]
        if not locs:
            raise Skip("couldn't find the file that produces this page in the repository")
        best = locs[0]
        if best.shared and not allow_shared:
            raise Skip(f"this page's head comes from a shared template ({best.path}); changing it there would change "
                       "every page, so it needs a per-page setting from your developer")
        return best.path

    def _need_llm(self) -> None:
        if self.llm is None:
            raise Skip("needs the AI service (switch it on in Settings)")

    async def _edit(self, path: str, item: FixItem, deterministic, instruction: str,
                    focus: list[str] | None = None) -> None:
        text = await self.prep.get(path)
        if text is None:
            raise Skip(f"{path} can't be read")
        new = deterministic(text) if deterministic and H.direct_editable(path) else None
        if new is None:
            self._need_llm()
            res = await ai_edit(self.llm, path=path, text=text, instruction=instruction, task=item.action,
                                focus=(focus or []) + ["</main", "<main", "<h1", "<body"])
            if res.text is None:
                raise Skip(res.reason)
            new = res.text
        if new != text:
            self.prep.put(path, new)

    def _facts(self, url: str | None, extra: str = "") -> str:
        p = self.pages.get(url or "", {})
        bits = [f"Business: {self.input.get('brand') or ''}", f"Website: {self.origin}",
                f"Page: {url}", f"Page title: {p.get('title') or ''}", f"Page description: {p.get('meta_description') or ''}",
                f"Page text (excerpt): {(self.texts.get(url or '') or '')[:3500]}"]
        if extra:
            bits.append(extra)
        return "\n".join(bits)

    # ------------------------------------------------------------------ main
    async def apply(self, item: FixItem) -> Outcome:
        before = set(self.prep.work)
        snapshot = dict(self.prep.work)
        try:
            out = await getattr(self, f"_do_{item.action}")(item) or Outcome()
        except Skip as e:
            self.prep.work = snapshot
            return Outcome("skipped", str(e))
        except AwaitingAnswer:
            self.prep.work = snapshot
            return Outcome("waiting", "waiting for the AI (manual mode): answer the saved request and prepare again")
        except LLMError as e:
            self.prep.work = snapshot
            return Outcome("failed", f"AI step failed: {e}")
        changed = [p for p in self.prep.work if p not in before or self.prep.work[p] != snapshot.get(p)]
        out.paths = changed
        if not changed and out.status == "done":
            out.status, out.reason = "skipped", out.reason or "already in place; nothing to change"
        return out

    # page head ---------------------------------------------------------
    async def _do_set_title(self, it: FixItem):
        v = it.data["value"]
        self.new_titles[it.page_url] = v
        await self._edit(await self._page_file(it.page_url), it, lambda t: H.set_title(t, v),
                         f"Set this page's HTML <title> (the title shown in Google) to exactly: {v}")

    async def _do_set_description(self, it: FixItem):
        v = it.data["value"]
        await self._edit(await self._page_file(it.page_url), it, lambda t: H.set_meta_description(t, v),
                         f'Set this page\'s meta description to exactly: {v}')

    async def _do_canonical(self, it: FixItem):
        href = it.data["href"]
        await self._edit(await self._page_file(it.page_url), it, lambda t: H.ensure_link(t, "canonical", href),
                         f'Add a canonical link for this page pointing to {href} (only if none exists).')

    async def _do_lang(self, it: FixItem):
        lang = it.data.get("lang") or self.answers.get("site_language")
        if not lang:
            raise Skip("needs your answer: which language the site is in")
        path = await self._page_file(it.page_url, allow_shared=True)  # <html lang> is site-wide by nature
        await self._edit(path, it, lambda t: H.set_html_lang(t, lang), f'Add lang="{lang}" to the <html> element.')

    async def _do_viewport(self, it: FixItem):
        path = await self._page_file(it.page_url, allow_shared=True)
        await self._edit(path, it, H.ensure_viewport,
                         'Add <meta name="viewport" content="width=device-width, initial-scale=1"> to the head.')

    async def _do_social_tags(self, it: FixItem):
        tags = dict(it.data.get("tags") or {})
        img = self.answers.get("og_image")
        if img:
            tags["og:image"] = img
        if not tags:
            raise Skip("no tag values available")
        path = await self._page_file(it.page_url)

        def det(t: str) -> str | None:
            for k, v in tags.items():
                t = H.set_meta(t, "property", k, v) if t is not None else None
            if t is not None and img:
                t = H.set_meta(t, "name", "twitter:card", "summary_large_image")
            return t
        await self._edit(path, it, det, "Add these Open Graph meta tags for this page: "
                         + "; ".join(f'{k}="{v}"' for k, v in tags.items()))

    async def _do_jsonld(self, it: FixItem):
        code = re.sub(r"</?script[^>]*>", "", it.data["code"]).strip()
        try:
            data = json.loads(code)
        except ValueError:
            raise Skip("the generated structured data isn't valid JSON")
        for q in it.questions:
            ans = self.answers.get(q["id"])
            if ans:
                data[q["field"]] = ({"@type": "PostalAddress", "streetAddress": ans} if q["field"] == "address" else ans)
            elif q.get("required"):
                raise Skip(f"needs your answer: {q['question']}")
        from app.modules.schema_rules import is_valid_jsonld
        ok, why = is_valid_jsonld(data)
        if not ok:
            raise Skip("structured data incomplete: " + "; ".join(why[:2]))
        block = '<script type="application/ld+json">\n' + json.dumps(data, indent=2, ensure_ascii=False) + "\n</script>"
        path = await self._page_file(it.page_url, allow_shared=it.data.get("type") in ("Organization", "WebSite"))
        await self._edit(path, it, lambda t: H.insert_jsonld(t, block),
                         "Add this structured data to this page's head exactly as given (in JSX use a script tag "
                         "with dangerouslySetInnerHTML and JSON.stringify of the object):\n" + block)

    # images (applied wherever the file appears) --------------------------
    async def _do_img_attrs(self, it: FixItem):
        src = it.data["src"]
        key = H._img_key(src)
        alt, w, h = it.data.get("alt"), it.data.get("width"), it.data.get("height")
        hits = 0
        for path in self.locator._scan_list():
            text = await self.prep.get(path)
            if not text or key not in text.lower():
                continue
            if H.direct_editable(path):
                new, n = H.set_img_attrs(text, src, alt=alt, width=w, height=h)
                if n:
                    self.prep.put(path, new)
                    hits += n
        if not hits:
            raise Skip("the image isn't referenced literally in the code (it may come from the CMS or a data file)")

    # site files ----------------------------------------------------------
    def _public(self, name: str) -> str:
        if self.fw.public_dir is None:
            raise Skip(f"{self.fw.label} sites can't add a {name} file from the repository")
        return f"{self.fw.public_dir}/{name}".lstrip("/")

    async def _do_sitemap(self, it: FixItem):
        if self.fw.generates_sitemap:
            raise Skip("the site already uses a sitemap plugin; check its configuration is enabled")
        path = self._public("sitemap.xml")
        if self.repo.exists(path):
            raise Skip(f"{path} already exists in the repository (it may not be deployed)")
        self.prep.put(path, H.sitemap_xml(it.data["urls"]))

    async def _do_robots_sitemap(self, it: FixItem):
        path = self._public("robots.txt")
        current = await self.prep.get(path)
        self.prep.put(path, H.append_robots_sitemap(current, f"{self.origin}/sitemap.xml"))

    async def _do_llms_txt(self, it: FixItem):
        path = self._public("llms.txt")
        if self.repo.exists(path):
            raise Skip("llms.txt already exists")
        home = self.pages.get(self.origin + "/") or next(iter(self.pages.values()), {})
        lines = [f"# {self.input.get('brand') or urlsplit(self.origin).netloc}", ""]
        if home.get("meta_description"):
            lines += [f"> {home['meta_description']}", ""]
        lines.append("## Pages")
        for p in sorted(self.page_list, key=lambda p: (p["url"].count("/"), p["url"]))[:40]:
            title = self.new_titles.get(p["url"]) or p.get("title")
            if p.get("indexable") and title:
                desc = f": {p['meta_description']}" if p.get("meta_description") else ""
                lines.append(f"- [{title}]({p['url']}){desc}")
        self.prep.put(path, "\n".join(lines) + "\n")
        return Outcome(text="\n".join(lines))

    # writing (reviewed by a person) --------------------------------------
    async def _do_write_opening(self, it: FixItem):
        self._need_llm()
        w = await write(self.llm, kind="text", task="opening", voice=self.voice, max_words=70,
                        brief="Write the opening paragraph for this page: 2–3 sentences that say plainly what the "
                              "business offers here and for whom, so a visitor (or an AI assistant) gets the answer "
                              "immediately. Here is a rough draft to improve:\n" + it.data["draft"],
                        facts=self._facts(it.page_url))
        path = await self._page_file(it.page_url)
        para = paragraphs_html(w.text)
        await self._edit(path, it, lambda t: H.insert_after_h1(t, para),
                         "Insert this paragraph as the first paragraph of the page's main content, right after the "
                         "main heading. Keep everything else:\n" + para, focus=["<h1"])
        return Outcome(text=w.text, issues=w.issues)

    async def _do_write_faq(self, it: FixItem):
        self._need_llm()
        pairs = dict(it.data.get("pairs") or {})
        for q in it.questions:
            if self.answers.get(q["id"]):
                pairs[q["faq"]] = self.answers[q["id"]]
        if len(pairs) < 2:
            raise Skip("needs at least two answered questions")
        extra = "Questions and the facts for their answers:\n" + "\n".join(f"Q: {q}\nA facts: {a}" for q, a in pairs.items())
        w = await write(self.llm, kind="faq", task="faq", voice=self.voice, max_words=60,
                        brief="Write a short FAQ: keep each question as a customer would ask it and answer in 1–3 "
                              "sentences using only the given facts.", facts=self._facts(it.page_url, extra))
        if len(w.faqs) < 2:
            raise Skip("the AI returned fewer than two usable answers")
        path = await self._page_file(it.page_url)
        block = faq_html(w.faqs)
        await self._edit(path, it, lambda t: H.insert_main_end(t, block),
                         "Add this FAQ section near the end of the page's main content (before the footer), matching "
                         "the surrounding markup style:\n" + block)
        text = await self.prep.get(path)
        if text and H.direct_editable(path):
            new = H.insert_jsonld(text, faq_jsonld(w.faqs))
            if new:
                self.prep.put(path, new)
        return Outcome(text="\n\n".join(f"Q: {f['question']}\nA: {f['answer']}" for f in w.faqs), issues=w.issues)

    async def _do_write_section(self, it: FixItem):
        q = it.questions[0]
        facts_from_owner = self.answers.get(q["id"])
        if not facts_from_owner:
            raise Skip("needs your answer (the facts for this section)")
        self._need_llm()
        w = await write(self.llm, kind="text", task="section", voice=self.voice, max_words=160,
                        brief=f"Write the body of a new section titled \"{it.data['heading']}\" for this page. "
                              f"Purpose: {it.data.get('what', '')}. Use the owner's notes as the main source.",
                        facts=self._facts(it.page_url, f"Owner's notes for this section: {facts_from_owner}"))
        path = await self._page_file(it.page_url)
        section = f"<section>\n  <h2>{H.esc(it.data['heading'])}</h2>\n  {paragraphs_html(w.text)}\n</section>"
        await self._edit(path, it, lambda t: H.insert_main_end(t, section),
                         "Add this section to the page's main content in the most sensible place (not inside the "
                         "header or footer), matching the surrounding markup style:\n" + section)
        return Outcome(text=w.text, issues=w.issues)

    async def _do_set_h1(self, it: FixItem):
        self._need_llm()
        h1 = it.data["h1"]
        path = await self._page_file(it.page_url)
        await self._edit(path, it, None, f"Make sure this page has exactly one main heading as an <h1> reading: "
                         f"\"{h1}\". If an empty <h1> exists, fill it. If the visible top heading uses another tag, "
                         "change that tag to <h1> and keep its classes/styling. Never add a second <h1>.", focus=["<h1"])
        return Outcome(text=h1)

    async def _do_fact_fix(self, it: FixItem):
        ans = self.answers.get(it.questions[0]["id"])
        if not ans:
            raise Skip("needs your answer: which value is correct")
        self._need_llm()
        path = await self._page_file(it.page_url, allow_shared=True)
        await self._edit(path, it, None, f"The page states contradictory facts: {it.data.get('problem')}. "
                         f"The owner says the correct information is: \"{ans}\". Update only the wrong value(s) in this "
                         f"file. Text found on the page: {it.before or ''}", focus=[(it.before or "")[:40]])

    async def _do_replace_placeholder(self, it: FixItem):
        ans = self.answers.get(it.questions[0]["id"])
        if not ans:
            raise Skip("needs your answer: what should replace the placeholder")
        self._need_llm()
        path = await self._page_file(it.page_url, allow_shared=True)
        if ans.lower() in ("remove", "delete"):
            await self._edit(path, it, None, f"Remove the placeholder text block that starts with: {it.data.get('found')}",
                             focus=[(it.data.get("found") or "")[:30]])
            return
        w = await write(self.llm, kind="text", task="placeholder", voice=self.voice, max_words=120,
                        brief="Turn the owner's notes into the text that replaces the placeholder on this page.",
                        facts=self._facts(it.page_url, f"Owner's notes: {ans}"))
        await self._edit(path, it, None, f"Replace the placeholder text block that starts with "
                         f"\"{it.data.get('found')}\" with:\n{paragraphs_html(w.text)}", focus=[(it.data.get("found") or "")[:30]])
        return Outcome(text=w.text, issues=w.issues)

    async def _do_add_link(self, it: FixItem):
        self._need_llm()
        target = _path(it.data["target"])
        path = await self._page_file(it.page_url)
        await self._edit(path, it, None, f"Add one contextual link to {target} with the link text \"{it.data['anchor']}\" "
                         "inside the page's main content, in a sentence where it fits naturally (you may add one short "
                         "sentence). Use the site's existing link markup. Don't add it to navigation or the footer.")

    async def _do_manual(self, it: FixItem):
        raise Skip("listed in the pull request as an instruction for your developer")

    # ------------------------------------------------------------------ run
    async def run(self, items: list[FixItem]) -> dict[str, Any]:
        order = {a: i for i, a in enumerate(AGENT_ORDER)}
        todo = sorted([i for i in items if i.kind != "manual"], key=lambda i: (order.get(i.agent, 99), i.label))
        results: dict[str, Outcome] = {}
        snapshots: list[tuple[str, dict[str, str]]] = []
        for agent in sorted({i.agent for i in todo}, key=lambda a: order.get(a, 99)):
            before = dict(self.prep.work)
            for it in [i for i in todo if i.agent == agent]:
                results[it.id] = await self.apply(it)
            changed = {p: t for p, t in self.prep.work.items() if before.get(p) != t}
            if changed:
                snapshots.append((agent, changed))
        # validate every changed file; a failing file is dropped with all items that touched it
        files, bad = [], set()
        for path, text in sorted(self.prep.work.items()):
            orig = self.prep.original.get(path)
            problems = check_file(path, orig, text)
            if problems:
                bad.add(path)
            diff = "".join(difflib.unified_diff((orig or "").splitlines(True), text.splitlines(True),
                                                f"a/{path}", f"b/{path}", n=2))
            files.append({"path": path, "new": orig is None, "problems": problems, "diff": diff[:60000]})
        for o in results.values():
            if set(o.paths) & bad:
                o.status, o.reason = "failed", "the change failed validation: " + "; ".join(
                    p for f in files if f["path"] in set(o.paths) & bad for p in f["problems"])[:300]
        commits = []
        for agent, changed in snapshots:
            keep = {p: t for p, t in changed.items() if p not in bad}
            if keep:
                n = sum(1 for i in todo if i.agent == agent and results[i.id].status == "done")
                commits.append({"agent": agent, "message": f"SEO: {agent} fixes ({n} change{'s' if n != 1 else ''})",
                                "files": keep})
        return {"framework": {"name": self.fw.name, "label": self.fw.label, "notes": self.fw.notes,
                              "public_dir": self.fw.public_dir},
                "repo": self.repo.label, "default_branch": self.repo.default_branch,
                "items": {k: {"status": v.status, "reason": v.reason, "paths": v.paths, "issues": v.issues,
                              "text": v.text} for k, v in results.items()},
                "files": [f for f in files], "commits": commits}


# ------------------------------------------------------------------ pull request
def pr_body(audit: dict, items: list[FixItem], result: dict) -> str:
    st = result["items"]
    done = [i for i in items if st.get(i.id, {}).get("status") == "done"]
    review = [i for i in done if i.kind in ("review", "input")]
    lines = [f"## SEO fixes from audit #{audit['id']} of {audit.get('domain')}", "",
             "Prepared by SEO Inspector from the audit report. **Nothing has been merged or deployed.** Please review "
             "the changes, especially the text marked *please read*, then merge when you're happy.", ""]
    by_agent: dict[str, list[FixItem]] = {}
    for i in done:
        by_agent.setdefault(f"{i.agent_icon} {i.agent}", []).append(i)
    lines.append("### What changed")
    for agent, its in by_agent.items():
        lines.append(f"**{agent}**")
        for i in its:
            mark = " — *please read*" if i in review else ""
            after = f": `{str(i.after)[:220]}`" if i.after and i.kind == "auto" else ""
            lines.append(f"- {i.label}{after}{mark}")
        lines.append("")
    if review:
        lines += ["### Text written for you (please read and edit)", ""]
        for i in review:
            text = st[i.id].get("text")
            if text:
                lines += [f"<details><summary>{i.label}</summary>", "", text, "", "</details>", ""]
            for issue in st[i.id].get("issues") or []:
                lines.append(f"> ⚠️ {i.label}: {issue}")
    skipped = [i for i in items if i.kind != "manual" and st.get(i.id, {}).get("status") in ("skipped", "failed", "waiting")]
    if skipped:
        lines += ["", "### Not changed", ""]
        lines += [f"- {i.label}: {st[i.id]['reason']}" for i in skipped[:60]]
    manual = [i for i in items if i.kind == "manual"]
    if manual:
        lines += ["", "### Still to do (needs a developer or a setting outside this repository)", ""]
        for i in manual[:40]:
            lines.append(f"- **{i.label}** — {i.data.get('do') or ''}")
    lines += ["", "---", f"_Generated by SEO Inspector · framework detected: {result['framework']['label']}_"]
    return "\n".join(lines)


async def publish(repo: Repo, *, audit: dict, items: list[FixItem], result: dict, branch: str) -> str:
    commits = [Commit(c["message"], c["files"]) for c in result.get("commits", []) if c.get("files")]
    if not commits:
        raise ValueError("No changes to publish")
    title = f"SEO fixes from audit #{audit['id']} ({sum(1 for v in result['items'].values() if v['status'] == 'done')} changes)"
    return await repo.publish(branch, commits, title, pr_body(audit, items, result))
