"""What kind of site is in the repository, and where its public files (sitemap, robots, llms.txt) belong."""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from app.fixer.repo import Repo


@dataclass
class Framework:
    name: str  # static-html | nextjs | astro | gatsby | nuxt | sveltekit | vite | eleventy | hugo | jekyll | shopify-theme | unknown
    label: str
    public_dir: str | None  # where files served at the site root live ("" = repo root); None = can't add files
    root: str = ""  # sub-folder that holds the site, when the repo has one
    notes: list[str] = field(default_factory=list)
    generates_sitemap: bool = False  # a plugin/config already builds sitemap.xml


LABELS = {"static-html": "Plain HTML", "nextjs": "Next.js", "astro": "Astro", "gatsby": "Gatsby", "nuxt": "Nuxt",
          "sveltekit": "SvelteKit", "vite": "Vite / React", "eleventy": "Eleventy", "hugo": "Hugo",
          "jekyll": "Jekyll", "shopify-theme": "Shopify theme", "unknown": "Unknown"}
PUBLIC = {"nextjs": "public", "astro": "public", "gatsby": "static", "nuxt": "public", "sveltekit": "static",
          "vite": "public", "hugo": "static", "jekyll": "", "eleventy": "", "static-html": ""}
SITEMAP_PLUGINS = ("next-sitemap", "@astrojs/sitemap", "gatsby-plugin-sitemap", "@nuxtjs/sitemap", "jekyll-sitemap",
                   "@quasibit/eleventy-plugin-sitemap", "vite-plugin-sitemap")


async def detect(repo: Repo) -> Framework:
    paths = repo.paths
    pset = set(paths)
    # the site may sit in a sub-folder (e.g. site/ or web/): use the shallowest folder with a package.json/index.html
    roots = sorted({p.rsplit("/", 1)[0] if "/" in p else "" for p in paths
                    if p.rsplit("/", 1)[-1] in ("package.json", "index.html", "hugo.toml", "_config.yml")},
                   key=lambda r: (r.count("/") if r else -1, r))
    root = roots[0] if roots else ""
    pre = f"{root}/" if root else ""
    deps: dict = {}
    pkg = await repo.read(pre + "package.json") if pre + "package.json" in pset else None
    if pkg:
        try:
            j = json.loads(pkg)
            deps = {**j.get("dependencies", {}), **j.get("devDependencies", {})}
        except ValueError:
            pass
    has = lambda *names: any(n in deps for n in names)
    if any(p.startswith(pre + "layout/theme.liquid") for p in paths) and any(p.startswith(pre + "templates/") for p in paths):
        name = "shopify-theme"
    elif has("next"):
        name = "nextjs"
    elif has("astro"):
        name = "astro"
    elif has("gatsby"):
        name = "gatsby"
    elif has("nuxt"):
        name = "nuxt"
    elif has("@sveltejs/kit"):
        name = "sveltekit"
    elif has("@11ty/eleventy"):
        name = "eleventy"
    elif has("vite", "react-scripts"):
        name = "vite"
    elif pre + "hugo.toml" in pset or (pre + "config.toml" in pset and any(p.startswith(pre + "layouts/") for p in paths)):
        name = "hugo"
    elif pre + "_config.yml" in pset:
        name = "jekyll"
    elif any(p.endswith((".html", ".htm")) for p in paths):
        name = "static-html"
    else:
        name = "unknown"
    pub = PUBLIC.get(name)
    fw = Framework(name=name, label=LABELS[name], root=root,
                   public_dir=None if pub is None else (f"{root}/{pub}".strip("/") if pub else root))
    if has(*SITEMAP_PLUGINS) or name == "shopify-theme":
        fw.generates_sitemap = True
    if name == "shopify-theme":
        fw.notes.append("Shopify themes can't add root files; page text, product descriptions and SEO titles set in "
                        "Shopify admin are not in this repository.")
    if name in ("nextjs", "nuxt", "sveltekit", "vite") and not fw.generates_sitemap:
        fw.notes.append("Head tags may be set in code (e.g. a metadata object); those edits are made by the AI editor "
                        "and need a careful review.")
    if repo.truncated:
        fw.notes.append("The repository is very large; GitHub returned only part of the file list.")
    return fw
