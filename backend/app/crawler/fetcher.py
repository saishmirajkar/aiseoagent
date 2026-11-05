"""Polite async HTTP fetcher.

* Custom User-Agent, per-site rate limiting, timeouts, retries with exponential backoff.
* Redirects are followed manually so the full chain (and loops) can be recorded.
* Bodies are streamed with a hard byte cap; callers needing whole files use ``stream()``.
"""
from __future__ import annotations

import asyncio
import random
import re
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from urllib.parse import urljoin

import httpx

from app.config import ssl_context
from app.crawler.rate_limit import HostRateLimiter

RETRY_STATUSES = {429, 502, 503, 504}
MAX_REDIRECTS = 10


@dataclass
class Hop:
    url: str
    status: int
    location: str | None


@dataclass
class FetchResult:
    url: str
    final_url: str
    status: int | None = None
    headers: dict[str, str] = field(default_factory=dict)
    chain: list[Hop] = field(default_factory=list)  # redirect hops before final_url
    elapsed_ms: int | None = None  # time to response headers of the final request
    body: bytes = b""
    truncated: bool = False
    error: str | None = None
    redirect_loop: bool = False
    too_many_redirects: bool = False
    blocked_redirect: bool = False  # a redirect target was blocked by robots.txt

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "").split(";")[0].strip().lower()

    @property
    def is_html(self) -> bool:
        ct = self.content_type
        return ct in ("text/html", "application/xhtml+xml") or (not ct and self.body.lstrip()[:15].lower().startswith((b"<!doctype html", b"<html")))

    def text(self) -> str:
        return decode_body(self.body, self.headers.get("content-type", ""))


_META_CHARSET = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?\s*([a-zA-Z0-9_\-]+)""", re.I)


def decode_body(body: bytes, content_type: str) -> str:
    if body.startswith(b"\xef\xbb\xbf"):
        return body[3:].decode("utf-8", errors="replace")
    charset = None
    m = re.search(r"charset=([\w\-]+)", content_type or "", re.I)
    if m:
        charset = m.group(1)
    else:
        mm = _META_CHARSET.search(body[:4096])
        if mm:
            charset = mm.group(1).decode("ascii", "ignore")
    for enc in filter(None, [charset, "utf-8"]):
        try:
            return body.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return body.decode("utf-8", errors="replace")


def _headers(resp: httpx.Response) -> dict[str, str]:
    out: dict[str, str] = {}
    for k in {k.lower() for k in resp.headers.keys()}:
        out[k] = ", ".join(resp.headers.get_list(k))
    return out


def _retry_after(resp: httpx.Response) -> float:
    try:
        return min(10.0, float(resp.headers.get("retry-after", "0")))
    except ValueError:
        return 0.0


class Fetcher:
    def __init__(self, user_agent: str, limiter: HostRateLimiter, *, timeout: float = 20.0,
                 retries: int = 2, max_bytes: int = 5_000_000,
                 allow: Callable[[str], bool] | None = None, verify: bool = True):
        self.user_agent = user_agent
        self.limiter = limiter
        self.timeout = timeout
        self.retries = retries
        self.max_bytes = max_bytes
        self.allow = allow
        self._verify = verify
        self._client: httpx.AsyncClient | None = None
        self.request_count = 0

    async def __aenter__(self) -> Fetcher:
        self._client = httpx.AsyncClient(
            headers={"User-Agent": self.user_agent,
                     "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                     "Accept-Language": "en;q=0.9,*;q=0.5"},
            timeout=httpx.Timeout(self.timeout, connect=min(10.0, self.timeout)),
            follow_redirects=False,
            verify=ssl_context() if self._verify else False,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            http2=False,
        )
        return self

    async def __aexit__(self, *exc) -> None:
        if self._client:
            await self._client.aclose()

    @property
    def client(self) -> httpx.AsyncClient:
        assert self._client is not None, "use 'async with Fetcher(...)'"
        return self._client

    async def _send(self, method: str, url: str, headers: dict | None) -> tuple[httpx.Response, int]:
        """One request (no redirects) with retries. Returns an open streaming response."""
        attempt = 0
        while True:
            await self.limiter.wait(url)
            t0 = time.perf_counter()
            try:
                self.request_count += 1
                req = self.client.build_request(method, url, headers=headers)
                resp = await self.client.send(req, stream=True)
            except (httpx.TransportError, httpx.InvalidURL) as e:
                if attempt >= self.retries or isinstance(e, httpx.InvalidURL):
                    raise
                attempt += 1
                await asyncio.sleep(2 ** (attempt - 1) + random.random() * 0.3)
                continue
            elapsed = int((time.perf_counter() - t0) * 1000)
            if resp.status_code in RETRY_STATUSES and attempt < self.retries:
                wait = max(_retry_after(resp), 2 ** attempt)
                await resp.aclose()
                attempt += 1
                await asyncio.sleep(wait)
                continue
            return resp, elapsed

    async def _follow(self, url: str, method: str, headers: dict | None,
                      follow: bool) -> tuple[FetchResult, httpx.Response | None]:
        result = FetchResult(url=url, final_url=url)
        current = url
        seen = {url}
        for _ in range(MAX_REDIRECTS + 1):
            try:
                resp, elapsed = await self._send(method, current, headers)
            except httpx.TimeoutException:
                result.error = "timeout"
                return result, None
            except (httpx.TransportError, httpx.InvalidURL) as e:
                result.error = f"{type(e).__name__}: {e}"[:300] or type(e).__name__
                return result, None
            result.final_url = current
            result.status = resp.status_code
            result.headers = _headers(resp)
            result.elapsed_ms = elapsed
            location = resp.headers.get("location")
            if follow and resp.status_code in (301, 302, 303, 307, 308) and location:
                nxt = urljoin(current, location.strip())
                result.chain.append(Hop(current, resp.status_code, nxt))
                await resp.aclose()
                if nxt in seen:
                    result.redirect_loop = True
                    result.final_url = nxt
                    return result, None
                if self.allow is not None and not self.allow(nxt):
                    result.blocked_redirect = True
                    result.final_url = nxt
                    return result, None
                seen.add(nxt)
                current = nxt
                if resp.status_code == 303:
                    method = "GET"
                continue
            return result, resp
        result.too_many_redirects = True
        return result, None

    async def fetch(self, url: str, *, method: str = "GET", read_body: bool = True,
                    max_bytes: int | None = None, headers: dict | None = None,
                    follow: bool = True, html_only: bool = False) -> FetchResult:
        """Fetch a URL. With ``html_only`` the body is only read for HTML responses."""
        result, resp = await self._follow(url, method, headers, follow)
        if resp is None:
            return result
        cap = max_bytes or self.max_bytes
        try:
            if read_body and method != "HEAD":
                ct = result.headers.get("content-type", "").lower()
                if not html_only or "html" in ct or not ct:
                    chunks: list[bytes] = []
                    size = 0
                    async for chunk in resp.aiter_bytes():
                        chunks.append(chunk)
                        size += len(chunk)
                        if size >= cap:
                            result.truncated = True
                            break
                    result.body = b"".join(chunks)[:cap]
        except httpx.TimeoutException:
            result.error = "timeout while reading body"
        except httpx.TransportError as e:
            result.error = f"{type(e).__name__} while reading body"
        finally:
            await resp.aclose()
        return result

    @asynccontextmanager
    async def stream(self, url: str, headers: dict | None = None
                     ) -> AsyncIterator[tuple[FetchResult, AsyncIterator[bytes] | None]]:
        """Stream a large body without buffering it; caller consumes the iterator."""
        result, resp = await self._follow(url, "GET", headers, True)
        try:
            yield result, (resp.aiter_bytes() if resp is not None else None)
        finally:
            if resp is not None:
                await resp.aclose()

    async def check(self, url: str) -> FetchResult:
        """Status check for link validation: HEAD, falling back to GET when HEAD is refused."""
        r = await self.fetch(url, method="HEAD", read_body=False)
        if r.error or r.status in (400, 403, 405, 406, 501) or r.status is None:
            r2 = await self.fetch(url, method="GET", read_body=False)
            if r2.status is not None or not r.status:
                return r2
        return r
