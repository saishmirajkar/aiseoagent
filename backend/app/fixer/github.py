"""Minimal GitHub REST client: read a repository and open a pull request from a new branch.

Uses the Git Data API (blobs → tree → commit → ref), so nothing is cloned to disk. The token needs
"Contents: read and write" and "Pull requests: read and write" on the one repository, nothing more.
"""
from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import ssl_context

API = "https://api.github.com"


class GitHubError(Exception):
    pass


@dataclass(frozen=True)
class RepoRef:
    owner: str
    name: str

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"


_REPO = re.compile(r"^(?:https?://)?(?:www\.)?(?:git@)?(?:github\.com[/:])?([A-Za-z0-9-]{1,39})/([A-Za-z0-9._-]{1,100}?)"
                   r"(?:\.git)?/?$")


def parse_repo(value: str) -> RepoRef:
    m = _REPO.match((value or "").strip())
    if not m:
        raise GitHubError("Enter the repository as https://github.com/owner/name or owner/name")
    return RepoRef(m.group(1), m.group(2))


class GitHubClient:
    def __init__(self, repo: RepoRef, token: str, *, client: httpx.AsyncClient | None = None, base: str = API):
        self.repo = repo
        self.base = base.rstrip("/")
        self._own = client is None
        self.http = client or httpx.AsyncClient(timeout=60.0, verify=ssl_context())
        self.headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                        "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "SEO-Inspector-Fixer"}

    async def close(self) -> None:
        if self._own:
            await self.http.aclose()

    async def _req(self, method: str, path: str, **kw) -> Any:
        url = f"{self.base}/repos/{self.repo.full_name}{path}"
        try:
            r = await self.http.request(method, url, headers=self.headers, **kw)
        except httpx.HTTPError as e:
            raise GitHubError(f"Could not reach GitHub ({type(e).__name__})") from e
        if r.status_code == 401:
            raise GitHubError("GitHub rejected the access token (expired or wrong)")
        if r.status_code == 403:
            raise GitHubError("The access token is not allowed to do this. It needs 'Contents: read and write' and "
                              "'Pull requests: read and write' on this repository.")
        if r.status_code == 404:
            raise GitHubError(f"Not found on GitHub: {self.repo.full_name}{path} (wrong name, or the token has no "
                              "access to this repository)")
        if r.status_code >= 400:
            msg = ""
            try:
                msg = r.json().get("message", "")
            except ValueError:
                pass
            raise GitHubError(f"GitHub error {r.status_code}: {msg}"[:300])
        return r.json() if r.content else None

    async def repo_info(self) -> dict:
        return await self._req("GET", "")

    async def branch_sha(self, branch: str) -> str:
        data = await self._req("GET", f"/git/ref/heads/{branch}")
        return data["object"]["sha"]

    async def tree(self, sha: str) -> tuple[list[dict], bool]:
        data = await self._req("GET", f"/git/trees/{sha}", params={"recursive": "1"})
        return [t for t in data.get("tree", []) if t.get("type") == "blob"], bool(data.get("truncated"))

    async def blob(self, sha: str) -> bytes:
        data = await self._req("GET", f"/git/blobs/{sha}")
        return base64.b64decode(data["content"]) if data.get("encoding") == "base64" else data["content"].encode()

    async def commit(self, parent_sha: str, files: dict[str, str], message: str) -> str:
        """Create a commit on top of parent_sha that writes ``files`` (path → text). Returns the commit sha."""
        parent = await self._req("GET", f"/git/commits/{parent_sha}")
        entries = []
        for path, text in sorted(files.items()):
            blob = await self._req("POST", "/git/blobs", json={"content": text, "encoding": "utf-8"})
            entries.append({"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]})
        tree = await self._req("POST", "/git/trees", json={"base_tree": parent["tree"]["sha"], "tree": entries})
        c = await self._req("POST", "/git/commits",
                            json={"message": message, "tree": tree["sha"], "parents": [parent_sha]})
        return c["sha"]

    async def create_branch(self, name: str, sha: str) -> None:
        await self._req("POST", "/git/refs", json={"ref": f"refs/heads/{name}", "sha": sha})

    async def move_branch(self, name: str, sha: str) -> None:
        await self._req("PATCH", f"/git/refs/heads/{name}", json={"sha": sha, "force": False})

    async def open_pr(self, head: str, base: str, title: str, body: str) -> str:
        data = await self._req("POST", "/pulls", json={"title": title, "head": head, "base": base, "body": body,
                                                         "maintainer_can_modify": True})
        return data["html_url"]
