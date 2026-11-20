"""Repository access for the fixer: GitHub (production) or a local checkout (tests/dev), same interface."""
from __future__ import annotations

import asyncio
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from app.fixer.github import GitHubClient, GitHubError

TEXT_EXT = {"html", "htm", "astro", "jsx", "tsx", "js", "ts", "mjs", "vue", "svelte", "md", "mdx", "liquid", "njk",
            "hbs", "ejs", "erb", "php", "twig", "xml", "txt", "json", "toml", "yml", "yaml"}
SKIP_DIRS = ("node_modules/", ".git/", "dist/", "build/", ".next/", "out/", "vendor/", ".cache/", "coverage/")
MAX_FILE = 600_000


def is_text_path(path: str) -> bool:
    ext = path.rsplit(".", 1)[-1].lower() if "." in path.rsplit("/", 1)[-1] else ""
    return ext in TEXT_EXT and not any(path.startswith(d) or f"/{d}" in path for d in SKIP_DIRS)


@dataclass
class Commit:
    message: str
    files: dict[str, str]  # path -> full new text


@dataclass
class Repo:
    """Read access plus one write operation: publish commits on a new branch (and open a PR when on GitHub)."""
    label: str
    default_branch: str
    paths: list[str] = field(default_factory=list)
    truncated: bool = False
    _cache: dict[str, str | None] = field(default_factory=dict)

    async def read(self, path: str) -> str | None:
        if path not in self._cache:
            self._cache[path] = await self._read(path)
        return self._cache[path]

    async def _read(self, path: str) -> str | None:
        raise NotImplementedError

    async def publish(self, branch: str, commits: list[Commit], title: str, body: str) -> str:
        raise NotImplementedError

    def exists(self, path: str) -> bool:
        return path in self._set

    @property
    def _set(self) -> set[str]:
        if not hasattr(self, "_pathset"):
            self._pathset = set(self.paths)
        return self._pathset


class GitHubRepo(Repo):
    def __init__(self, client: GitHubClient, default_branch: str, head_sha: str, blobs: dict[str, dict], truncated: bool):
        super().__init__(label=f"github.com/{client.repo.full_name}", default_branch=default_branch,
                         paths=sorted(blobs), truncated=truncated)
        self.client, self.head_sha, self.blobs = client, head_sha, blobs

    @classmethod
    async def open(cls, client: GitHubClient) -> GitHubRepo:
        info = await client.repo_info()
        branch = info.get("default_branch") or "main"
        sha = await client.branch_sha(branch)
        entries, truncated = await client.tree(sha)
        return cls(client, branch, sha, {e["path"]: e for e in entries}, truncated)

    async def _read(self, path: str) -> str | None:
        e = self.blobs.get(path)
        if e is None or (e.get("size") or 0) > MAX_FILE:
            return None
        try:
            return (await self.client.blob(e["sha"])).decode("utf-8")
        except UnicodeDecodeError:
            return None

    async def publish(self, branch: str, commits: list[Commit], title: str, body: str) -> str:
        if not commits:
            raise GitHubError("Nothing to publish")
        current = await self.client.branch_sha(self.default_branch)  # base on the latest default branch
        sha = current
        for c in commits:
            sha = await self.client.commit(sha, c.files, c.message)
        await self.client.create_branch(branch, sha)
        return await self.client.open_pr(branch, self.default_branch, title, body)


class LocalRepo(Repo):
    """A git checkout on disk. Publishing commits to a new local branch (no pull request)."""

    def __init__(self, root: str | Path):
        root = Path(root)
        paths = sorted(str(p.relative_to(root)).replace(os.sep, "/") for p in root.rglob("*")
                       if p.is_file() and ".git" not in p.parts)
        branch = self._git(root, "rev-parse", "--abbrev-ref", "HEAD", check=False) or "main"
        super().__init__(label=f"local:{root}", default_branch=branch, paths=paths)
        self.root = root

    @staticmethod
    def _git(root: Path, *args: str, check: bool = True) -> str:
        r = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)
        if check and r.returncode != 0:
            raise GitHubError(f"git {' '.join(args)} failed: {r.stderr.strip()[:300]}")
        return r.stdout.strip()

    async def _read(self, path: str) -> str | None:
        p = self.root / path
        if not p.is_file() or p.stat().st_size > MAX_FILE:
            return None
        try:
            return p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return None

    async def publish(self, branch: str, commits: list[Commit], title: str, body: str) -> str:
        def run() -> str:
            self._git(self.root, "checkout", "-q", "-b", branch)
            for c in commits:
                for path, text in c.files.items():
                    f = self.root / path
                    f.parent.mkdir(parents=True, exist_ok=True)
                    f.write_text(text, encoding="utf-8")
                    self._git(self.root, "add", path)
                self._git(self.root, "-c", "user.name=SEO Inspector", "-c", "user.email=fixer@seo-inspector.local",
                          "commit", "-q", "-m", c.message)
            self._git(self.root, "checkout", "-q", self.default_branch)
            return f"local branch {branch} in {self.root}"
        return await asyncio.to_thread(run)
