"""Find the source file(s) that produce an audited page.

1. By path: /about → about.html, about/index.html, src/pages/about.astro, app/about/page.tsx, content/about.md…
2. By content: files containing the page's exact title, meta description or H1.
A file that matches many different pages (a layout, _document, theme.liquid) is marked shared: editing a
per-page value there would change every page, so such edits go to the AI editor or to manual steps.
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from urllib.parse import unquote, urlsplit

from app.fixer.detect import Framework
from app.fixer.repo import Repo, is_text_path

SOURCE_EXT = ("html", "htm", "astro", "jsx", "tsx", "js", "ts", "vue", "svelte", "md", "mdx", "liquid", "njk", "hbs",
              "ejs", "erb", "php", "twig")
LAYOUT_HINT = re.compile(r"(^|/)(_?layouts?|_includes|partials|components/(head|seo|layout)|theme\.liquid|_document|"
                         r"_app|base\.|default\.|baseof\.|head\.)", re.I)
MAX_CONTENT_SCAN = 400


@dataclass
class PageInfo:
    url: str
    title: str | None = None
    meta_description: str | None = None
    h1: str | None = None


@dataclass
class Location:
    path: str
    score: int
    reasons: list[str] = field(default_factory=list)
    shared: bool = False


def path_candidates(url: str, fw: Framework) -> list[str]:
    path = unquote(urlsplit(url).path or "/").strip("/")
    pre = f"{fw.root}/" if fw.root else ""
    pub = f"{fw.public_dir}/" if fw.public_dir else ""
    slug = path or "index"
    out = []
    if not path:
        out += [f"{pre}index.html", f"{pub}index.html", f"{pre}index.htm", f"{pre}src/index.html", f"{pre}index.md",
                f"{pre}src/pages/index.astro", f"{pre}pages/index.tsx", f"{pre}pages/index.jsx", f"{pre}pages/index.js",
                f"{pre}src/pages/index.tsx", f"{pre}src/pages/index.jsx", f"{pre}app/page.tsx", f"{pre}app/page.jsx",
                f"{pre}src/app/page.tsx", f"{pre}content/_index.md", f"{pre}src/routes/+page.svelte",
                f"{pre}templates/index.liquid", f"{pre}templates/index.json"]
    else:
        base = path[:-5] if path.endswith((".html")) else path
        out += [f"{pre}{base}.html", f"{pre}{base}/index.html", f"{pub}{base}.html", f"{pub}{base}/index.html",
                f"{pre}{base}.htm", f"{pre}{base}.md", f"{pre}{base}/index.md", f"{pre}src/pages/{base}.astro",
                f"{pre}src/pages/{base}/index.astro", f"{pre}src/pages/{base}.md", f"{pre}pages/{base}.tsx",
                f"{pre}pages/{base}.jsx", f"{pre}pages/{base}.js", f"{pre}pages/{base}/index.tsx",
                f"{pre}src/pages/{base}.tsx", f"{pre}src/pages/{base}.jsx", f"{pre}app/{base}/page.tsx",
                f"{pre}app/{base}/page.jsx", f"{pre}src/app/{base}/page.tsx", f"{pre}content/{base}.md",
                f"{pre}content/{base}/index.md", f"{pre}_posts/{slug}.md", f"{pre}src/routes/{base}/+page.svelte"]
    return list(dict.fromkeys(out))


def _needles(info: PageInfo) -> list[tuple[str, str, int]]:
    out = []
    for label, value, weight in (("title", info.title, 3), ("meta description", info.meta_description, 3),
                                 ("H1", info.h1, 2)):
        if value and len(value.strip()) >= 4:
            v = value.strip()
            for form in {v, html.escape(v, quote=False), v.replace("&", "&amp;"), v.replace("'", "&#39;"),
                         v.replace("’", "'")}:
                out.append((label, form, weight))
    return out


class Locator:
    def __init__(self, repo: Repo, fw: Framework):
        self.repo, self.fw = repo, fw
        self._scan: list[str] | None = None
        self.hits: dict[str, set[str]] = {}  # path -> urls it matched by content

    def _scan_list(self) -> list[str]:
        if self._scan is None:
            pre = f"{self.fw.root}/" if self.fw.root else ""
            files = [p for p in self.repo.paths if is_text_path(p) and p.startswith(pre)
                     and p.rsplit(".", 1)[-1].lower() in SOURCE_EXT]
            # likely page/template folders first, then shallow files
            prio = lambda p: (0 if re.search(r"(^|/)(src|app|pages|templates|layouts|content|sections|_includes)/", p)
                              or p.count("/") <= (pre.count("/") + 1) else 1, p.count("/"), p)
            self._scan = sorted(files, key=prio)[:MAX_CONTENT_SCAN]
        return self._scan

    async def locate(self, info: PageInfo) -> list[Location]:
        found: dict[str, Location] = {}
        for p in path_candidates(info.url, self.fw):
            if self.repo.exists(p):
                found[p] = Location(p, 5, ["path matches the page address"])
        needles = _needles(info)
        if needles:
            for p in (list(found) + [x for x in self._scan_list() if x not in found]):
                text = await self.repo.read(p)
                if not text:
                    continue
                matched = {label: w for label, form, w in needles if form in text}
                if matched:
                    loc = found.setdefault(p, Location(p, 0))
                    loc.score += sum(matched.values())
                    loc.reasons += [f"contains the page's {label}" for label in matched]
                    self.hits.setdefault(p, set()).add(info.url)
        out = sorted(found.values(), key=lambda l: (-l.score, l.path))
        for loc in out:
            loc.shared = bool(LAYOUT_HINT.search(loc.path))
        return out

    def mark_shared(self, locs: list[Location]) -> None:
        """After locating every page: a file whose content matched several different pages is a shared template."""
        for loc in locs:
            if len(self.hits.get(loc.path, ())) > 1 and not any("path matches" in r for r in loc.reasons):
                loc.shared = True
