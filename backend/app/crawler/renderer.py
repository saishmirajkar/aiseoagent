"""Headless Chromium rendering, used only when raw HTML is missing content.

Memory budget: exactly one browser, one page at a time (guarded by a lock), launched
lazily and closed as soon as the crawl finishes.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
from dataclasses import dataclass, field

from app.config import env
from app.crawler.rate_limit import HostRateLimiter

log = logging.getLogger(__name__)

_IMAGE_JS = """
() => Array.from(document.images).map(img => {
  const r = img.getBoundingClientRect();
  return {src: img.currentSrc || img.src, w: Math.round(r.width), h: Math.round(r.height),
          nw: img.naturalWidth, nh: img.naturalHeight, top: r.top + window.scrollY};
})
"""


@dataclass
class RenderResult:
    html: str = ""
    status: int | None = None
    final_url: str | None = None
    images: list[dict] = field(default_factory=list)
    viewport_h: int = 900
    error: str | None = None


def _chromium_path() -> str | None:
    p = env().playwright_chromium_path
    if p:
        return p
    if os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
        return None  # Playwright resolves its own bundled browser
    for name in ("chromium", "chromium-browser", "google-chrome"):
        found = shutil.which(name)
        if found:
            return found
    return None


class Renderer:
    def __init__(self, user_agent: str, limiter: HostRateLimiter, timeout_s: float = 25.0):
        self.user_agent = user_agent
        self.limiter = limiter
        self.timeout_ms = int(timeout_s * 1000)
        self._pw = None
        self._browser = None
        self._context = None
        self._lock = asyncio.Lock()
        self.render_count = 0
        self.unavailable: str | None = None

    async def _ensure(self) -> bool:
        if self._context is not None:
            return True
        if self.unavailable:
            return False
        try:
            from playwright.async_api import async_playwright
            self._pw = await async_playwright().start()
            self._browser = await self._pw.chromium.launch(
                executable_path=_chromium_path(),
                args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"])
            self._context = await self._browser.new_context(
                user_agent=self.user_agent, viewport={"width": 1366, "height": 900},
                java_script_enabled=True, ignore_https_errors=False)
            return True
        except Exception as e:
            self.unavailable = f"Headless browser unavailable: {type(e).__name__}: {e}"[:300]
            log.warning(self.unavailable)
            await self.close()
            return False

    async def render(self, url: str, *, load_images: bool = False) -> RenderResult:
        async with self._lock:
            if not await self._ensure():
                return RenderResult(error=self.unavailable)
            page = await self._context.new_page()
            blocked = {"media", "font"} if load_images else {"media", "font", "image"}

            async def _route(route):
                if route.request.resource_type in blocked:
                    await route.abort()
                else:
                    await route.continue_()

            await page.route("**/*", _route)
            out = RenderResult()
            try:
                await self.limiter.wait(url)
                resp = await page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
                try:
                    await page.wait_for_load_state("networkidle", timeout=min(10_000, self.timeout_ms))
                except Exception:
                    pass  # long-polling sites never go idle; DOM is good enough
                out.status = resp.status if resp else None
                out.final_url = page.url
                out.html = await page.content()
                if load_images:
                    out.images = await page.evaluate(_IMAGE_JS)
                self.render_count += 1
            except Exception as e:
                out.error = f"{type(e).__name__}: {e}"[:300]
            finally:
                await page.close()
            return out

    async def close(self) -> None:
        for obj, meth in ((self._context, "close"), (self._browser, "close"), (self._pw, "stop")):
            if obj is not None:
                try:
                    await getattr(obj, meth)()
                except Exception:
                    pass
        self._context = self._browser = self._pw = None
